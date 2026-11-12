"""The only way anything merges.

One process, start to finish: check every gate, take the repository's merge
lock, check again, squash-merge the exact commit that was graded, wait for
GitHub to say MERGED, release the lock. A manager that dies mid-merge leaves
a token file behind, and `--release-own` at the next session start frees it.

The lock is LoopRunner's push-CAS on `refs/looprunner/merge-lock`, with the
same commit message shape and the same abandonment rule, so a LoopRunner
instance and a Herdr manager working one repository exclude each other. A
holder writes an orphan commit to the ref with a plain push; the remote
rejects every second writer. A lock commit older than an hour is abandoned
and may be replaced under `--force-with-lease`.

`--after-build` runs the part of the gate that needs no grade, the moment a
builder says it is done: the pull request exists, is open, is not armed to
merge itself, carries the contract, and neither it nor any commit on it
carries AI attribution. A fault found there costs a message to the builder
instead of a grade.

`--outside <pr>` merges a pull request from outside the program instead of
an item: one a steward on this host labeled `rollout-ready`, under the same
lock and every gate that does not come from a review, and only where
program.json says `"outside_prs": "adopt"` (hl/outside.py says why and how).
While an outside merge's deploy check has not passed, no item merges into
that repository on top of it.

Exit codes: 0 merged (or dry run or after-build check passed), 2 refused by a
gate, 3 lock not acquired in time, 4 branch is behind and must be updated,
5 merge did not complete.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from . import claim, confirm, contractfmt, items, review, verdict
from .common import Refusal, absolute, emit, read_json, sha256_text, utc_now, write_json

LOCK_REF = "refs/looprunner/merge-lock"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
LOCK_ABANDON_S = 60 * 60
LOCK_WAIT_S = 45 * 60  # LoopRunner holds through its whole merge wait, up to 40 min
LOCK_POLL_S = 15
MERGE_WAIT_S = 10 * 60
MERGE_POLL_S = 10
MERGEABLE_STATES = ("CLEAN", "UNSTABLE", "HAS_HOOKS")
# How often GitHub is asked again for a merge state that says BLOCKED beside checks that are green, and the
# seconds between two looks.
BLOCKED_LOOKS = 4
BLOCKED_PAUSE_S = 5

ATTRIBUTION_RE = re.compile(
    r"co-authored-by:.*\b(claude|cursor|codex|copilot|anthropic|openai)"
    r"|generated with \[?(claude|cursor|codex)"
    r"|made with \[?cursor"
    r"|\U0001F916 generated",
    re.IGNORECASE,
)

# Who a commit says wrote it. A tool that commits under its own name shows
# here even when its message is clean.
IDENTITY_RE = re.compile(
    r"cursoragent@cursor\.com|noreply@anthropic\.com|^(cursor agent|cursor|claude|claude code|codex|copilot)\s*<",
    re.IGNORECASE,
)

PR_FIELDS = (
    "number,url,state,isDraft,title,body,headRefName,headRefOid,baseRefName,"
    "mergeStateStatus,autoMergeRequest,mergeCommit"
)


class LockTimeout(Exception):
    def __init__(self, holder: str, waited_s: float):
        self.holder = holder
        super().__init__(f"merge lock still held by {holder} after {int(waited_s)}s")


# Every git call the kit makes for a program offers the loop's own token and nothing else. The host's credential
# helpers are left out (the empty value clears the list), and the one that is left gives GH_TOKEN, which
# `require_loop_token` took from the keeper. On the mini the host's helper would otherwise answer with the
# operator's keyring token wherever GH_TOKEN was not in the environment. The token is never on a command line.
LOOP_CREDENTIAL = ("-c", "credential.helper=", "-c", 'credential.helper=!f() { test "$1" = get && echo username=x-access-token && echo "password=$GH_TOKEN"; }; f')


class Tools:
    """The two binaries this script trusts, resolved once. With --root they
    are the program's pinned git and gh; nothing here searches PATH twice."""

    def __init__(self, git: str | None = None, gh: str | None = None, env=None, sleep=time.sleep):
        self.git_bin = git or shutil.which("git")
        self.gh_bin = gh or shutil.which("gh")
        if not self.git_bin or not self.gh_bin:
            raise Refusal("git and gh must both be available (pass --git and --gh)")
        self.env = dict(os.environ if env is None else env)
        self.sleep = sleep

    def _run(self, exe, args, cwd, check, env=None, stdin=None, before=()):
        proc = subprocess.run(
            [exe, *before, *args], cwd=str(cwd), env=env or self.env, input=stdin, capture_output=True, text=True
        )
        if check and proc.returncode != 0:
            name = Path(exe).name
            raise RuntimeError(f"{name} {' '.join(args[:4])} failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
        return proc

    def git(self, args, cwd, check=True, env=None, stdin=None):
        # Never a prompt: with no token to give, a call that needs one fails, and says so.
        env = {**(env or self.env), "GIT_TERMINAL_PROMPT": "0"}
        return self._run(self.git_bin, args, cwd, check, env=env, stdin=stdin, before=LOOP_CREDENTIAL)

    def gh(self, args, cwd, check=True):
        return self._run(self.gh_bin, args, cwd, check)


# ---------------------------------------------------------------- lock


def lock_remote_sha(tools: Tools, repo, ref: str) -> str | None:
    line = tools.git(["ls-remote", "origin", ref], repo).stdout.strip()
    return line.split()[0] if line else None


def _lock_commit(tools: Tools, repo, ref: str, sha: str) -> tuple[float, str] | None:
    """Age in seconds and message of the lock commit, read from the commit
    itself: its writer may be another machine. None when the ref vanished
    between the listing and the fetch."""
    if tools.git(["fetch", "origin", ref], repo, check=False).returncode != 0:
        return None
    shown = tools.git(["show", "-s", "--format=%ct%n%B", sha], repo, check=False)
    if shown.returncode != 0:
        return None
    epoch, _, body = shown.stdout.partition("\n")
    return time.time() - int(epoch.strip()), body


def _holder(body: str, sha: str) -> str:
    run_id = re.search(r"^run_id: (.+)$", body, re.MULTILINE)
    item_id = re.search(r"^item_id: (.+)$", body, re.MULTILINE)
    return f"{run_id.group(1)}/{item_id.group(1)}" if run_id and item_id else sha[:12]


def _write_lock_commit(tools: Tools, repo, run_id: str, item_id: str) -> str:
    message = f"looprunner merge-lock\n\nrun_id: {run_id}\nitem_id: {item_id}\nacquired_at: {utc_now()}\n"
    env = dict(tools.env)
    for role in ("AUTHOR", "COMMITTER"):
        env[f"GIT_{role}_NAME"] = "herdr-loop"
        env[f"GIT_{role}_EMAIL"] = "herdr-loop@localhost"
    return tools.git(["commit-tree", EMPTY_TREE, "-m", message], repo, env=env).stdout.strip()


def acquire_lock(
    tools: Tools,
    repo,
    run_id: str,
    item_id: str,
    ref: str = LOCK_REF,
    wait_s: float = LOCK_WAIT_S,
    poll_s: float = LOCK_POLL_S,
    abandon_s: float = LOCK_ABANDON_S,
) -> dict:
    started = time.monotonic()
    holder = "unknown"
    while True:
        prior = lock_remote_sha(tools, repo, ref)
        lease = None
        fresh = False
        if prior is not None:
            seen = _lock_commit(tools, repo, ref, prior)
            if seen is not None:
                age, body = seen
                holder = _holder(body, prior)
                if age <= abandon_s:
                    fresh = True
                else:
                    lease = prior
        if not fresh:
            new = _write_lock_commit(tools, repo, run_id, item_id)
            argv = ["push", "origin", f"{new}:{ref}"]
            if lease:
                argv.append(f"--force-with-lease={ref}:{lease}")
            if tools.git(argv, repo, check=False).returncode == 0:
                return {
                    "ref": ref,
                    "held_sha": new,
                    "reclaimed_from": lease,
                    "run_id": run_id,
                    "item_id": item_id,
                    "acquired_at": utc_now(),
                }
        waited = time.monotonic() - started
        if waited >= wait_s:
            raise LockTimeout(holder, waited)
        tools.sleep(poll_s)


def release_lock(tools: Tools, repo, token: dict) -> bool:
    """Delete the ref, but only while it still holds the commit this process
    pushed. If another holder reclaimed it, the push is rejected and nothing
    of theirs is touched."""
    lease = f"--force-with-lease={token['ref']}:{token['held_sha']}"
    return tools.git(["push", "origin", lease, f":{token['ref']}"], repo, check=False).returncode == 0


def release_own(tools: Tools, repo, run_dir, ref: str = LOCK_REF) -> list[str]:
    """Free anything this run still holds: token files left by a merge that
    died, and a lock commit on the remote that names this run."""
    run = items.load_run(run_dir)
    freed: list[str] = []
    for token_path in sorted(Path(run_dir).glob("items/*/merge-lock.json")):
        token = read_json(token_path)
        if release_lock(tools, repo, token):
            freed.append(f"{token['item_id']}: released {token['held_sha'][:12]}")
        token_path.unlink()
    sha = lock_remote_sha(tools, repo, ref)
    if sha:
        seen = _lock_commit(tools, repo, ref, sha)
        if seen and re.search(rf"^run_id: {re.escape(run['run_id'])}$", seen[1], re.MULTILINE):
            if release_lock(tools, repo, {"ref": ref, "held_sha": sha}):
                freed.append(f"remote lock {sha[:12]} named this run: released")
    return freed


# ---------------------------------------------------------------- gates


def _attribution(label: str, text: str) -> str | None:
    m = ATTRIBUTION_RE.search(text or "")
    return f"{label} carries AI attribution: {m.group(0)[:60]!r}" if m else None


def commit_attribution(tools: Tools, repo, base_ref: str, head: str) -> list[str]:
    """Attribution in any commit the pull request adds, read from the commits
    themselves. A tool can add its trailer on the command line whatever its
    settings say, and GitHub copies a commit's co-authors into the squash
    commit whatever message the merge is given."""
    fmt = "%H%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e"
    log = tools.git(["log", f"--format={fmt}", f"{base_ref}..{head}"], repo).stdout
    found = []
    for entry in log.split("\x1e"):
        if not entry.strip():
            continue
        sha, author, committer, body = entry.strip("\n").split("\x1f", 3)
        hit = _attribution(f"commit {sha[:12]}", body)
        if hit:
            found.append(hit)
        for role, who in (("author", author), ("committer", committer)):
            if IDENTITY_RE.search(who):
                found.append(f"commit {sha[:12]} has the {role} {who!r}")
    return found


def _fetch_pr(tools: Tools, repo, view: dict) -> str | None:
    """Bring the pull request's base and head into the clone, as `origin/<branch>`. Returns why not, if it fails.
    Each is named with where it goes: a clone told to follow only the base and the item branches would
    otherwise fetch a train's branch and leave `origin/<train>`, which the gate reads, where it was."""
    specs = [f"+refs/heads/{branch}:refs/remotes/origin/{branch}" for branch in (view["baseRefName"], view["headRefName"])]
    proc = tools.git(["fetch", "origin", *specs], repo, check=False)
    if proc.returncode != 0:
        return f"could not fetch the pull request's branches: {proc.stderr.strip()[:200]}"
    return None


def _pr_problems(tools: Tools, repo, view: dict, branch: str) -> list[str]:
    """What is wrong with the pull request itself, whatever was graded: its
    state, who may merge it, and attribution in its text, diff and commits.
    Expects the pull request's branches to be fetched."""
    reasons = []
    if view["state"] != "OPEN":
        reasons.append(f"pull request #{view['number']} is {view['state']}, not OPEN")
    if view.get("isDraft"):
        reasons.append("pull request is a draft")
    if view.get("autoMergeRequest"):
        reasons.append("auto-merge is armed on the pull request; nothing but this script merges")
    if view["headRefName"] != branch:
        reasons.append(f"pull request head is {view['headRefName']!r}, expected {branch!r}")
    for label, text in (("pull request title", view.get("title")), ("pull request body", view.get("body"))):
        found = _attribution(label, text)
        if found:
            reasons.append(found)
    if view["state"] != "OPEN":
        return reasons

    try:
        reasons += commit_attribution(tools, repo, f"origin/{view['baseRefName']}", view["headRefOid"])
    except RuntimeError as exc:
        reasons.append(f"could not read the pull request's commits: {exc}")
    try:
        diff = _pr_diff(tools, repo, f"origin/{view['baseRefName']}", view["headRefOid"])
    except RuntimeError as exc:
        reasons.append(f"could not read the pull request diff: {exc}")
    else:
        added = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        found = _attribution("the diff", added)
        if found:
            reasons.append(found)
    return reasons


def _pr_diff(tools: Tools, repo, base_ref: str, head: str) -> str:
    """The pull request's diff, as GitHub shows it: from the merge base to the head.
    Read from the clone, not `gh pr diff`: GitHub gives no diff of more than 300 files.
    Rename detection is asked for, as GitHub does, so the clone's own settings change nothing."""
    merge_base = tools.git(["merge-base", base_ref, head], repo).stdout.strip()
    return tools.git(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", merge_base, head], repo).stdout


def _worktree_problems(tools: Tools, worktree: str | None, head: str) -> list[str]:
    """What the builder's worktree holds that the pull request does not. The
    build is served and graded from the worktree, so a grade is only about
    the pull request when the two are the same."""
    if not worktree:
        return []
    if not Path(worktree).is_dir():
        return [f"the worktree {worktree} does not exist"]
    at = tools.git(["rev-parse", "HEAD"], worktree, check=False)
    status = tools.git(["status", "--porcelain"], worktree, check=False)
    if at.returncode != 0 or status.returncode != 0:
        return [f"could not read the worktree {worktree}: {(at.stderr or status.stderr).strip()[:200]}"]
    reasons = []
    if at.stdout.strip() != head:
        reasons.append(
            f"the worktree is at {at.stdout.strip()[:12]} but the pull request's head is {head[:12]}: push what you committed"
        )
    left = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    if left:
        shown = ", ".join(left[:10]) + (f" and {len(left) - 10} more" if len(left) > 10 else "")
        reasons.append(f"the worktree is not clean: {shown}")
    hidden = review.hidden_files(tools, worktree)
    if hidden:
        # `git status` says nothing of these, whatever was done to them: the mark is what is refused.
        shown = ", ".join(hidden[:10]) + (f" and {len(hidden) - 10} more" if len(hidden) > 10 else "")
        clear = " and ".join(f"`git update-index {option} -- <path>`" for option in review.UNMARK)
        reasons.append(
            f"git was told not to look at tracked files in the worktree ({shown}: marked assume-unchanged or skip-worktree, by hand or by a sparse "
            "checkout, which this worktree may not be), so nothing shows they are as the pull request has them: switch a sparse checkout off "
            f"(`git sparse-checkout disable`) and clear the marks ({clear}, one command for each mark), then commit and push a change that belongs to "
            "the item, or put the file back (`git checkout HEAD -- <path>`)"
        )
    return reasons


def _patch_id(tools: Tools, repo, base_ref: str, sha: str) -> str:
    merge_base = tools.git(["merge-base", base_ref, sha], repo).stdout.strip()
    diff = tools.git(["diff", merge_base, sha], repo).stdout
    # --verbatim: white space is part of a patch. Without it a line moved out of a block by its indentation alone is "the same patch".
    out = tools.git(["patch-id", "--verbatim"], repo, stdin=diff).stdout.split()
    return out[0] if out else ""


def _pr_view(tools: Tools, repo, slug: str, pr: str) -> dict:
    for _ in range(6):
        data = json.loads(tools.gh(["pr", "view", str(pr), "--repo", slug, "--json", PR_FIELDS], repo).stdout)
        # GitHub computes mergeability lazily; UNKNOWN means "ask again".
        if data.get("mergeStateStatus") != "UNKNOWN" or data.get("state") != "OPEN":
            return data
        tools.sleep(5)
    return data


# What says whether a pull request conflicts with its base, and the head and base it says it of.
CONFLICT_FIELDS = "headRefOid,baseRefName,mergeable,mergeStateStatus"


def conflicts(view: dict) -> bool:
    """Whether GitHub says a pull request conflicts with its base: it is not
    mergeable (`CONFLICTING`), or its merge state is `DIRTY`. GitHub runs no
    `pull_request` workflow for such a head, so its checks never report and
    a wait for them waits for nothing; the merge gate refuses it as DIRTY.
    `UNKNOWN` (GitHub has not worked it out yet) is not a conflict."""
    return view.get("mergeable") == "CONFLICTING" or view.get("mergeStateStatus") == "DIRTY"


def conflict_next(item_id: str, build: int, base: str) -> str:
    """What a pull request that conflicts with its base needs: a rework the manager writes the reason for."""
    return (
        f"the pull request of {item_id} conflicts with `{base}`: a rework. Write in `rework-{build + 1}.md` that the builder merges "
        f"`origin/{base}` into its branch (never a rebase or a force-push) and resolves the conflict, then start build {build + 1}; "
        "the head it pushes gets a review of its own"
    )


# A required check GitHub has not heard from yet: listed as pending, as GitHub itself holds the merge for it.
def not_reported(name: str) -> dict:
    return {"name": name, "bucket": "pending", "state": "NOT_REPORTED", "link": ""}


def required_names(tools: Tools, repo, slug: str, base: str) -> list[str]:
    """Every check the base branch requires by name: its branch protection's
    and the rules active on it, as GitHub requires both. A branch with
    neither (GitHub answers 404) requires none."""
    branch = urllib.parse.quote(base, safe="")
    reads = (
        (f"repos/{slug}/branches/{branch}/protection/required_status_checks", "[(.checks // [])[].context] + (.contexts // [])"),
        (f"repos/{slug}/rules/branches/{branch}", '[.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context]'),
    )
    names: set[str] = set()
    for path, jq in reads:
        proc = tools.gh(["api", path, "--jq", jq], repo, check=False)
        if proc.returncode == 0:
            names.update(json.loads(proc.stdout or "[]"))
        elif "HTTP 404" not in proc.stderr:
            raise RuntimeError(f"reading the checks {base} requires failed: {proc.stderr.strip()[:300]}")
    return sorted(names)


def required_checks(tools: Tools, repo, slug: str, pr: str, base: str | None = None) -> list[dict]:
    """The required checks of a pull request, each one the base branch
    requires: as reported, or pending when it has not reported yet.
    `gh pr checks --required` lists only the required checks that have
    reported, so a required check that starts late (a help-desk program's
    CI check, which reports once its shards finish) was missing, and the
    list read green before it was (a decision in the help-desk catalog
    program's ledger)."""
    proc = tools.gh(
        ["pr", "checks", str(pr), "--repo", slug, "--required", "--json", "name,bucket,state,link"], repo, check=False
    )
    text = proc.stdout.strip()
    if text:
        listed = json.loads(text)
    elif "no required checks" in proc.stderr.lower() or "no checks" in proc.stderr.lower():
        listed = []
    else:
        raise RuntimeError(f"gh pr checks failed: {proc.stderr.strip()[:300]}")
    if base is None:
        base = json.loads(tools.gh(["pr", "view", str(pr), "--repo", slug, "--json", "baseRefName"], repo).stdout)["baseRefName"]
    seen = {c.get("name") for c in listed}
    return listed + [not_reported(name) for name in required_names(tools, repo, slug, base) if name not in seen]


def item_checks(tools: Tools, repo, slug: str, pr: str, run: dict, base: str | None = None) -> tuple[bool, list[dict]]:
    """The checks an item's own pull request must show green, and whether
    the run asks for any at all.

    A run that merges into the base asks for the repository's required
    checks, as GitHub lists them. A run on a train asks for the checks its
    repository names for an item (`item_checks` in the spec), which may be
    none: GitHub requires nothing of a pull request into the train, so they
    are read from everything the pull request reports, and one that has not
    reported yet is listed as pending."""
    mode = items.policy(run)
    if mode["merge"] != "train":
        return True, required_checks(tools, repo, slug, pr, base)
    wanted = mode["train"].get("item_checks") or []
    if not wanted:
        return False, []
    proc = tools.gh(["pr", "checks", str(pr), "--repo", slug, "--json", "name,bucket,state,link"], repo, check=False)
    text = proc.stdout.strip()
    said = proc.stderr.lower()
    if not text and "no checks" not in said and "no required checks" not in said:
        raise RuntimeError(f"gh pr checks failed: {proc.stderr.strip()[:300]}")
    reported = {c["name"]: c for c in (json.loads(text) if text else [])}
    return True, [reported.get(name) or not_reported(name) for name in wanted]


def blocked_reason(what: str, listed: list) -> str:
    """Why a pull request GitHub calls blocked is not merged although no
    check listed for it is red. Every check the base branch requires is
    listed, a missing one as pending, so GitHub is waiting for something
    else the branch requires. GitHub would refuse the merge itself; a gate
    that had called it green would still have been wrong."""
    seen = "every required check it lists is green" if listed else "it lists no required check"
    return (
        f"GitHub reports {what} blocked (merge state BLOCKED) although {seen}. The base branch requires something "
        'else that has not happened, such as an approving review (troubles.md, "GitHub says blocked")'
    )


def settled_merge_state(tools: Tools, repo, slug: str, pr) -> str:
    """A merge state that said BLOCKED, read again after the checks. The
    state a gate holds was read before the checks were, and GitHub works it
    out a moment after a check changes: beside checks that have just turned
    green it can still say what it said before. It is asked a few times,
    and BLOCKED is believed only if no look says anything else. A state
    GitHub is still working out is not something else."""
    for look in range(BLOCKED_LOOKS):
        if look:
            tools.sleep(BLOCKED_PAUSE_S)
        seen = json.loads(tools.gh(["pr", "view", str(pr), "--repo", slug, "--json", "mergeStateStatus"], repo).stdout)
        if seen.get("mergeStateStatus") not in ("BLOCKED", "UNKNOWN"):
            return seen.get("mergeStateStatus")
    return "BLOCKED"


def gate(
    run_dir,
    item_id: str,
    repo,
    tools: Tools,
    pr: str | None = None,
    allow_no_required_checks=False,
    train_ahead: tuple[int, int] | None = None,
    repo_facts: dict | None = None,
    root=None,
) -> dict:
    """Every condition a merge needs, checked together. Raises Refusal
    naming all that fail. Returns what the merge step needs.

    `train_ahead`, for a run on a train: how many merged items the train
    holds that have not been promoted, and the limit past which nothing
    more merges into it.

    A head merges on a passing full review of it, or on a passing delta
    confirm of it that follows a failed full review of an ancestor. For the
    second, everything that let the confirm start is read again here, from
    git and the run's records (`confirm.chain_problems`). `repo_facts` is
    the repository's entry in program.json, which that reading needs."""
    run = items.load_run(run_dir)
    on_train = items.on_train(run)
    item = items.load_item(run_dir, item_id)
    attempts = items.load_attempts(run_dir, item_id)
    d = items.item_dir(run_dir, item_id)
    reasons: list[str] = []
    warnings: list[str] = []

    if item["status"] != "in_progress":
        raise Refusal(f"{item_id} is {item['status']}; only an item in progress can merge")

    live = [e for e in attempts["log"] if not e.get("voided")]
    grades = [e for e in live if e["phase"] == "grade"]
    if not grades:
        raise Refusal(f"{item_id} has not been graded")
    graded = grades[-1]

    style = items.policy(run)["review"]
    result = verdict.check(d, graded["n"], style=style)
    confirming = graded.get("kind") == items.CONFIRM
    # A confirm that cannot stand is followed by a full review of the same head, and every refusal of one says so.
    in_full = f"; review the head in full (`hl-flow review --id {item_id} --full`)" if confirming else ""
    reasons += [f"verdict-{graded['n']}.json: {p}{in_full}" for p in result["problems"]]
    if not result["problems"] and result["effective"] != "PASS":
        detail = f" ({result['downgraded']})" if result["downgraded"] else ""
        reasons.append(f"the latest grade is {result['effective']}, not PASS{detail}")
    if style == "code":
        # A review that read the code and ran nothing has shown nothing about what the build does.
        # Read from the reviewer's own transcript here, at the gate, and held beside what the first read made of
        # it: the record of what was served lies where a worker can write, and what that read found stands.
        kept = (graded.get("read") or {}).get("exercise") or []
        now = review.check(run_dir, item_id, graded["n"], tools, repo)["problems"]
        reasons += [f"review {graded['n']}: {p}" for p in kept + [p for p in now if p not in kept]]
    # What the review did to its checkout: read once by `hl-flow reviewed`, when the review was over, against
    # the state noted when it began, and kept on the review's attempt. Nothing can read it again later, and a
    # pass over a checkout that moved or changed under the reviewer is not a pass of the commit that merges.
    # A run whose reviews read the code is always read that way. A grade of the kit's first kind may have been
    # read by hand, with no such read on its attempt, and merges as it always did.
    read = graded.get("read")
    if read is None and style == "code":
        reasons.append(f"review {graded['n']} has not been read: `hl-flow reviewed --id {item_id}` reads it against its transcript and its checkout, and a merge rests on what that finds")
    elif read and read.get("checkout") and read.get("effective", result["effective"]) == "PASS":
        # Only a pass, by the word that was read: a review that did not pass is refused for what it is, and stands.
        more = f" (and {len(read['checkout']) - 1} more)" if len(read["checkout"]) > 1 else ""
        reasons.append(f"review {graded['n']} does not count for a merge: {read['checkout'][0]}{more}; {items.changed_pass_next(run_dir, item_id, graded['n'])}")
    if read and "verdict_sha" in read and read["verdict_sha"] != verdict.file_sha(d, graded["n"]):
        # What that read took from the verdict is on the attempt; the file is anybody's to rewrite who can write the run directory.
        reasons.append(
            f"verdict-{graded['n']}.json is not the verdict `hl-flow reviewed` read: it was changed afterwards, or is gone. Nothing merges on it: "
            f"review the head again (`hl-flow review --id {item_id}{' --full' if confirming else ''}`)"
        )

    contract_path = d / "contract.md"
    if not contract_path.exists() or sha256_text(contract_path.read_text()) != graded.get("contract_sha"):
        reasons.append("contract.md is not the contract that was graded; grade again")
    rewritten = items.contract_problem(run_dir, item_id)
    if rewritten:
        reasons.append(rewritten)
    if graded["build"] != attempts["build"]:
        reasons.append(f"build {attempts['build']} has not been graded (the latest grade is of build {graded['build']})")

    builders = {e["session"] for e in live if e["phase"] == "build"} | {
        e["drafter_session"] for e in live if e.get("drafter_session")
    }
    if not graded.get("session") or graded["session"] in builders:
        reasons.append("the evaluator's session is missing or is also a builder's session")
    for phase, bound in items.bounds(run).items():
        # Grades are bounded per build, and a correction buys one more.
        allowed = (bound + attempts["correction"]) * max(attempts["build"], 1) if phase == "grade" else bound
        # Reviews given back after a pass are counted where they stay, in the log.
        count = len(items.given_back(attempts)) if phase == items.GIVE_BACK else attempts[phase]
        if count > allowed:
            reasons.append(f"{phase} count {count} is past its bound of {allowed}")

    if on_train and train_ahead and train_ahead[0] >= train_ahead[1]:
        reasons.append(
            f"the train holds {train_ahead[0]} merged items that have not been promoted, and its limit is {train_ahead[1]}: "
            "test and promote a batch (`hl-train`), or repair the one that is red, before anything more merges into it"
        )
    # A merge whose deploy check has not passed may be a red deploy. Nothing else of the run lands on top of it.
    # On a train nothing is deployed by an item's merge; the same rule is held where a batch is promoted.
    for other_id in [] if on_train else run["order"]:
        if other_id == item_id:
            continue
        other = items.load_item(run_dir, other_id)
        if other["status"] == "merged" and other.get("verify_command") and other["fields"].get("verify") not in items.VERIFY_SETTLED:
            reasons.append(
                f"{other_id} is merged and its Verify is not recorded as passed (verify={other['fields'].get('verify')}); "
                "nothing else of this run merges until it is: run `hl-verify` (after a fix-forward has merged and verified, `hl-verify --fixed-by`)"
            )
    # The same holds for an outside pull request this program merged into the repository (`hl-merge --outside`),
    # or began to merge and has no record of. A program that adopts none has no `outside/` directory in any run,
    # and nothing is read for it.
    program_root = None if on_train else _root_of(root, run_dir)
    if program_root is not None:
        from . import outside  # here, not at the top: outside.py is built on this module

        # An item the manager recorded as the fix-forward of a failed outside deploy (`hl-items fixes-outside`, guarded
        # as `exempt` is) is the one merge that hold lets through, and the answer says so.
        fixing = (item.get("fixes_outside") or {}).get("pr")
        held = outside.pending(program_root, run["repo"], fixing=fixing)
        reasons += [f"{what}: nothing else merges into {run['repo']} until it has passed" for what in held]
        if fixing is not None and len(held) < len(outside.pending(program_root, run["repo"])):
            warnings.append(f"merging past outside pull request #{fixing}'s failed deploy check, as its fix-forward: {item['fixes_outside'].get('reason')}")

    pr = pr or item["fields"].get("pr")
    branch = item["fields"].get("branch")
    if not pr:
        reasons.append("no pull request recorded: set --field pr=<number> or pass --pr")
    if not branch:
        reasons.append("no branch recorded: set --field branch=<name>")
    if reasons:
        raise Refusal(reasons)

    slug = run["repo"]
    view = _pr_view(tools, repo, slug, pr)
    if view["state"] == "MERGED":
        # The base has taken the pull request in, so where its merge base was can no longer be asked of the base.
        unsound = confirm.chain_problems(run_dir, item_id, graded, tools, repo, None, repo_facts) if confirming else []
        if unsound:
            raise Refusal(unsound)
        return _already_merged(tools, repo, view, graded)
    fetch_problem = _fetch_pr(tools, repo, view) if view["state"] == "OPEN" else None
    if fetch_problem:
        raise Refusal(fetch_problem)
    reasons += _pr_problems(tools, repo, view, branch)
    if confirming:
        reasons += confirm.chain_problems(run_dir, item_id, graded, tools, repo, f"origin/{view['baseRefName']}", repo_facts)
    if on_train and view.get("baseRefName") != items.policy(run)["train"]["branch"]:
        # Merged where it points, it would reach the base without the repository's checks ever having run on it.
        reasons.append(
            f"the pull request's base is `{view.get('baseRefName')}`; this run's items merge into `{items.policy(run)['train']['branch']}`"
        )

    head = view["headRefOid"]
    if view["state"] == "OPEN":
        # Read again here, not only after the build: an exemption withdrawn since then stops this merge, and so does a
        # path the spec protects now.
        reasons += protected_problems(tools, repo, run, item, view, repo_facts)[0]
    head_changed = head != graded["head_sha"]
    if head_changed and view["state"] == "OPEN":
        base_ref = f"origin/{view['baseRefName']}"
        try:
            same = _patch_id(tools, repo, base_ref, head) == _patch_id(tools, repo, base_ref, graded["head_sha"])
        except RuntimeError as exc:
            same = False
            warnings.append(f"could not compare patches: {exc}")
        if same:
            warnings.append(f"head moved from {graded['head_sha'][:12]} to {head[:12]} with an identical patch")
        else:
            reasons.append(
                f"head is {head[:12]} but {graded['head_sha'][:12]} was graded, and the patch differs: grade the new head"
            )

    state = view.get("mergeStateStatus")
    asked, checks = item_checks(tools, repo, slug, pr, run, view.get("baseRefName"))
    not_green = [f"{c['name']}={c.get('bucket') or c.get('state')}" for c in checks if c.get("bucket") not in ("pass", "skipping")]
    # Where BLOCKED is GitHub's word and nothing else says why. A draft is blocked for being one, and checks
    # that are not green say so themselves. A train's branch is unprotected, so an item's pull request into it
    # is not held to this.
    unexplained = not on_train and view["state"] == "OPEN" and not view.get("isDraft") and not not_green
    if state == "BLOCKED" and unexplained:
        state = settled_merge_state(tools, repo, slug, pr)
    needs_update = state == "BEHIND"
    if state == "DIRTY":
        reasons.append("pull request conflicts with its base; the builder must resolve it and it must be graded again")
    elif state not in MERGEABLE_STATES and not needs_update and view["state"] == "OPEN":
        warnings.append(f"merge state is {state}")

    if asked and not checks and not allow_no_required_checks:
        reasons.append("no required checks are reported for this pull request")
    if not_green:
        reasons.append(("the checks this repository asks of an item" if on_train else "required checks") + " are not green: " + ", ".join(sorted(not_green)))
    elif state == "BLOCKED" and unexplained:
        # GitHub's own word on the pull request, which no list of checks overrides: not a green one, and not
        # an empty one that the caller said to expect.
        reasons.append(blocked_reason("the pull request", checks))

    subject = f"{item_id}: {item['title']} (#{view['number']})"
    passed = f"Graded PASS at {head[:12]} (grade {graded['n']})"
    if confirming:
        passed = f"Review {graded['confirms']} was a full review of {graded['from_sha'][:12]} and failed it; review {graded['n']} confirmed the fix at {head[:12]}"
    body = f"Item {item_id} of run {run['run_id']}.\n{passed}, contract {graded['contract_sha'][:12]}.\n"
    found = _attribution("the squash message", subject + "\n" + body)
    if found:
        reasons.append(found)

    if reasons:
        raise Refusal(reasons)
    if read and read.get("dirty_before"):
        # Said every time: the spec's word is all that lets such a file through, and the spec is a file too.
        warnings.append(f"review {graded['n']} ran over tracked files that differed from the commit, which the spec names under `rewrites`: {', '.join(read['dirty_before'][:8])}")
    return {
        "pr": view["number"],
        "url": view.get("url"),
        "head_sha": head,
        "graded_sha": graded["head_sha"],
        "graded_n": graded["n"],
        "merge_state": state,
        "needs_update": needs_update,
        "subject": subject,
        "body": body,
        "warnings": warnings,
    }


def _already_merged(tools: Tools, repo, view: dict, graded: dict) -> dict:
    """The pull request is merged and the item does not say so: a merge that
    died between GitHub's answer and its own record, or a merge made outside
    this script. Everything the item itself must show (a PASS, an unchanged
    contract, separate sessions, bounds) has been checked by now, so what is
    left to ask is whether the commit that landed is the one that was graded."""
    head, graded_sha = view["headRefOid"], graded["head_sha"]
    same = head == graded_sha
    if not same:
        # A merged branch is deleted; its last commit is still reachable under the pull request's own ref.
        tools.git(["fetch", "origin", f"refs/pull/{view['number']}/head"], repo, check=False)
        base_ref = f"origin/{view['baseRefName']}"
        try:
            same = _patch_id(tools, repo, base_ref, head) == _patch_id(tools, repo, base_ref, graded_sha)
        except RuntimeError:
            same = False
    if not same:
        raise Refusal(
            f"pull request #{view['number']} was merged outside this script at {head[:12]}, which is not the commit "
            f"that was graded ({graded_sha[:12]}). Nothing here records that as a merge: mark the item failed "
            "(`operator: merged outside the gate`) and tell the operator"
        )
    return {
        "already_merged": True,
        "pr": view["number"],
        "url": view.get("url"),
        "head_sha": head,
        "graded_sha": graded_sha,
        "graded_n": graded["n"],
        "merge_sha": (view.get("mergeCommit") or {}).get("oid"),
        "needs_update": False,
        "warnings": [f"pull request #{view['number']} was already merged; this run only recorded it"],
    }


# ---------------------------------------------------------------- after a build


class NotFinished(Refusal):
    """The builder said done and the build is not: things only it can fix."""


def _squash(text: str | None) -> str:
    return " ".join((text or "").split())


# gh rewrites control characters in what it reads from GitHub, `--json` and `--jq` alike, into caret
# notation: a raw ESC becomes `^[`, and so does the escape `\u001b` written as text, after its backslash
# (`\^[`). GitHub stores the text as it was sent. These are the code points it does this to (gh 2.100):
# as an escape, C0 apart from tab, line feed, U+000B and carriage return, and all of C1; as a raw
# character, the same less backspace and form feed.
_GH_ESCAPE_RE = re.compile(r"\\u(00[0189][0-9A-Fa-f])")
_GH_ESCAPE_KEPT = {0x09, 0x0A, 0x0B, 0x0D}
_GH_RAW_RE = re.compile("[\x00-\x07\x0e-\x1f\x80-\x9f]")


def _caret(code: int) -> str:
    return "^" + chr((code & 0x1F) + 0x40)


def _as_gh_reads(text: str | None) -> str:
    """`text` as gh gives it back after GitHub has stored it. A body and a
    contract are both put in this form before one is looked for in the
    other, so the check holds whether or not gh rewrote the body. A text gh
    has already read comes out the same, unless gh's own `\\^\\` (for
    `\\u001c`) is followed by `u` and an escape's hex digits."""

    def escape(match: re.Match) -> str:
        code = int(match.group(1), 16)
        return match.group(0) if code in _GH_ESCAPE_KEPT else "\\" + _caret(code)

    return _GH_RAW_RE.sub(lambda m: _caret(ord(m.group(0))), _GH_ESCAPE_RE.sub(escape, text or ""))


def protected_problems(tools: Tools, repo, run: dict, item: dict, view: dict, repo_facts: dict | None) -> tuple[list[str], list[str]]:
    """Which protected paths the pull request changes that this item has no
    exemption for, as reasons; and which it changes that it has one for.
    The exemptions are the item's record as it stands now (`items.granted`),
    read at the check after a build and again at the merge: a record
    withdrawn or changed in between is what the merge goes by."""
    protected = (repo_facts or {}).get("protected")
    if not protected:
        return [], []
    try:
        touched = review.diff_facts(tools, repo, f"origin/{view['baseRefName']}", view["headRefOid"], {"protected": protected})["protected"]
    except RuntimeError as exc:
        return [f"could not read which paths the pull request changes: {exc}"], []
    # Only a path the manager named for this item, with its reason (`hl-items exempt`), is let through; nothing else is.
    allowed = items.granted(run, item)
    refused = [path for path in touched if path not in allowed]
    also = f" (of the protected paths, this item may change only {', '.join(sorted(allowed))})" if allowed else ""
    reasons = [f"the pull request changes paths no item may change: {', '.join(refused)}{also}"] if refused else []
    return reasons, [path for path in touched if path in allowed]


def after_build(run_dir, item_id: str, repo, tools: Tools, pr: str | None = None, repo_facts: dict | None = None) -> dict:
    """Check a finished build before a grade is spent on it, and record its
    pull request and head commit on the item. `repo_facts` is the
    repository's entry in program.json: a pull request that changes a path
    its spec says no item may change goes back to the builder."""
    run = items.load_run(run_dir)
    item = items.load_item(run_dir, item_id)
    d = items.item_dir(run_dir, item_id)
    n = items.load_attempts(run_dir, item_id)["build"]
    if item["status"] != "in_progress" or n == 0:
        raise Refusal(f"{item_id} is {item['status']} with {n} builds; there is no build to check")
    branch = item["fields"].get("branch")
    if not branch:
        raise Refusal("no branch recorded: set --field branch=<name> before the build starts")
    if not (d / f"build-{n}.done").exists():
        raise Refusal(f"build-{n}.done does not exist: the builder has not finished")
    rewritten = items.contract_problem(run_dir, item_id)
    if rewritten:
        # Not the builder's to put right: the contract is the manager's to install, and a build is measured against the installed one.
        raise Refusal(rewritten)

    reasons: list[str] = []
    how: dict = {}
    report = d / f"build-{n}.md"
    if not report.exists():
        reasons.append(f"{report} is missing: write the build report the brief describes")
    else:
        try:
            how = contractfmt.how_to_run(report.read_text())
        except contractfmt.ContractError as exc:
            reasons.append(f"{report.name}: {exc}")
        else:
            if (item.get("route") or {}).get("look") and contractfmt.noop_serve(how["serve"]):
                reasons.append(
                    f"{report.name}: this item is routed with a look, and its Serve line is `{how['serve'].strip()}`, which starts nothing. "
                    "A reviewer cannot look at a build that is not running: put the command that starts the real server on the port "
                    "in your brief in the Serve line, with a URL in the Ready line"
                )

    slug = run["repo"]
    pr = pr or item["fields"].get("pr")
    if not pr:
        listing = tools.gh(["pr", "list", "--repo", slug, "--head", branch, "--state", "open", "--json", "number"], repo)
        found = json.loads(listing.stdout)
        if len(found) != 1:
            raise NotFinished(reasons + [f"expected exactly one open pull request from branch {branch}, found {len(found)}"])
        pr = str(found[0]["number"])
    view = _pr_view(tools, repo, slug, pr)
    fetch_problem = _fetch_pr(tools, repo, view) if view["state"] == "OPEN" else None
    if fetch_problem:
        raise Refusal(fetch_problem)
    reasons += _pr_problems(tools, repo, view, branch)
    if items.on_train(run) and view.get("baseRefName") != items.policy(run)["train"]["branch"]:
        train_branch = items.policy(run)["train"]["branch"]
        reasons.append(
            f"the pull request's base is `{view.get('baseRefName')}`; this run's items go into `{train_branch}`: change the base "
            f"(`gh api --method PATCH repos/{slug}/pulls/{view['number']} -f base={train_branch}`)"
        )
    reasons += _worktree_problems(tools, item["fields"].get("worktree"), view["headRefOid"])
    exempted: list[str] = []
    if view["state"] == "OPEN":
        problems, exempted = protected_problems(tools, repo, run, item, view, repo_facts)
        reasons += problems

    # Before whitespace is squashed: NEL and U+001C..U+001F are whitespace to split(), and their caret forms are not.
    body = _squash(_as_gh_reads(view.get("body")))
    criteria = contractfmt.parse_criteria((d / "contract.md").read_text())
    absent = [k for k, text in criteria if _squash(_as_gh_reads(text)) not in body]
    if absent:
        reasons.append(
            f"the pull request body does not carry the contract word for word: criteria {absent} are not in it"
        )
    if reasons:
        raise NotFinished(reasons)

    pairs = [f"pr={view['number']}", f"pr_url={view.get('url')}", f"head_sha={view['headRefOid']}"]
    items.set_fields(run_dir, item_id, pairs)
    out = {"result": "built", "build": n, "pr": view["number"], "url": view.get("url"), "head_sha": view["headRefOid"], **how}
    if exempted:
        # Said every time: the record is the only thing that lets these through, and the record is a file too.
        out["protected_exempted"] = exempted
    return out


def send_back(run_dir, item_id: str, reasons: list[str]) -> dict:
    """Set the done file aside and write what the builder must fix. The
    builder's pane is still open; the manager sends it the one line returned
    here and waits for the done file again."""
    d = items.item_dir(run_dir, item_id)
    n = items.load_attempts(run_dir, item_id)["build"]
    done = d / f"build-{n}.done"
    k = len(list(d.glob(f"build-{n}.done.refused-*"))) + 1
    fix = d / f"build-{n}.fix-{k}.md"
    lines = [
        f"# Build {n} of {item_id} is not finished",
        "",
        f"Your done file was set aside: a check found the faults below. Fix every one, then create the empty file `{done}` again and end your turn.",
        "",
        *[f"- {reason}" for reason in reasons],
        "",
        "## Rules for this fix",
        "",
        "- Change nothing the code does. This is about the pull request, its commits, your worktree and your report.",
        "- A worktree that is not clean: commit and push a file that belongs to the item; delete anything else your commands left behind (`__pycache__/`, a coverage file, a log). A file git tracks that something else rewrote (an install, the build while it was served for a review) is not the item's: put it back as your last commit has it (`git checkout HEAD -- <path>`). `git status --porcelain` must then print nothing. Scratch output goes under `.scratch/`, which this clone ignores.",
        "- A commit that carries an attribution line (`Co-authored-by` naming a tool, `Generated with`) must be rewritten without it. For this purpose only, you may rewrite the commits of your own branch and push with `git push --force-with-lease`. Never pass `--trailer` to `git commit`. Check with `git log --format=%B origin/HEAD..HEAD`.",
        "- A pull request body that lacks the contract: replace the body with the contract file verbatim, then the two Serve/Ready lines, then your self-check table.",
        "- A path no item may change (a generated or pinned file): put it back as the base branch has it (`git checkout origin/<base> -- <path>`), commit, and push. If the item cannot be built without changing it, say so in your report under `## Proposed items` and build the rest.",
        "- Do not merge, close or approve the pull request, and do not enable auto-merge. If auto-merge is on, turn it off: `gh pr merge --disable-auto <number>`.",
        "",
    ]
    fix.write_text("\n".join(lines))
    done.rename(d / f"build-{n}.done.refused-{k}")
    return {
        "result": "sent-back",
        "build": n,
        "times_sent_back": k,
        "refused": reasons,
        "fix_file": str(fix),
        "prompt": f"Read {fix} and do exactly what it says.",
    }


# ---------------------------------------------------------------- merge


def _record_merged(run_dir, item_id: str, repo, tools: Tools, slug: str, record: dict, merge_sha: str | None) -> dict:
    """Write down a merge GitHub has made, and make the item merged."""
    record.update(state="MERGED", merge_sha=merge_sha, at=utc_now())
    landed = tools.gh(["api", f"repos/{slug}/commits/{merge_sha}", "--jq", ".commit.message"], repo, check=False)
    # Nothing can take it back now; the manager files it and tells the operator.
    record["attribution_on_base"] = _attribution("the merged commit", landed.stdout) if landed.returncode == 0 else None
    write_json(items.item_dir(run_dir, item_id) / "merge.json", record)
    items.set_status(run_dir, item_id, "merged", f"merged: PR #{record['pr']} as {merge_sha}")
    return {"result": "merged", **record}


def merge(run_dir, item_id: str, repo, tools: Tools, opts) -> tuple[int, dict]:
    run = items.load_run(run_dir)
    d = items.item_dir(run_dir, item_id)
    # The gate reads the protected paths, and a delta confirm's fix against the paths the program's spec names.
    kwargs = {
        "pr": opts.pr,
        "allow_no_required_checks": opts.allow_no_required_checks,
        "repo_facts": _facts_if_known(getattr(opts, "root", None), run_dir),
        "root": getattr(opts, "root", None),
    }
    if items.on_train(run) and not run.get("repair"):
        # A run that repairs the train must be able to merge into a full one: it is what empties it.
        kwargs["train_ahead"] = _train_ahead(getattr(opts, "root", None), run_dir, run)

    # Everything slow happens before the lock: the lock covers the merge only.
    ctx = gate(run_dir, item_id, repo, tools, **kwargs)
    if ctx.get("already_merged"):
        if opts.dry_run:
            return 0, {"result": "already-merged", **ctx}
        record = {"pr": ctx["pr"], "head_sha": ctx["head_sha"], "graded_n": ctx["graded_n"], "recovered": True}
        result = _record_merged(run_dir, item_id, repo, tools, run["repo"], record, ctx["merge_sha"])
        # A merge that died holding the lock left it held; nothing else of this run can be merging now.
        result["released"] = release_own(tools, repo, run_dir, ref=opts.lock_ref)
        return 0, {**result, "warnings": ctx["warnings"]}
    if ctx["needs_update"]:
        return 4, {"result": "needs-update", **ctx}
    if opts.dry_run:
        return 0, {"result": "dry-run-passed", **ctx}

    try:
        token = acquire_lock(
            tools, repo, run["run_id"], item_id, ref=opts.lock_ref, wait_s=opts.lock_wait, poll_s=opts.lock_poll
        )
    except LockTimeout as exc:
        return 3, {"result": "lock-timeout", "holder": exc.holder, "detail": str(exc)}
    token_path = d / "merge-lock.json"
    write_json(token_path, token)

    try:
        ctx = gate(run_dir, item_id, repo, tools, **kwargs)
        if ctx.get("already_merged"):
            # It merged while this run waited for the lock.
            record = {"pr": ctx["pr"], "head_sha": ctx["head_sha"], "graded_n": ctx["graded_n"], "recovered": True}
            result = _record_merged(run_dir, item_id, repo, tools, run["repo"], record, ctx["merge_sha"])
            return 0, {**result, "warnings": ctx["warnings"]}
        if ctx["needs_update"]:
            return 4, {"result": "needs-update", **ctx}
        merge_argv = ["pr", "merge", str(ctx["pr"]), "--repo", run["repo"], "--merge" if run.get("land") == "merge" else "--squash"]
        merge_argv += ["--match-head-commit", ctx["head_sha"]]
        merge_argv += ["--subject", ctx["subject"], "--body", ctx["body"]]
        merged = tools.gh(merge_argv, repo, check=False)
        record = {"pr": ctx["pr"], "head_sha": ctx["head_sha"], "graded_n": ctx["graded_n"], "at": utc_now()}
        if merged.returncode != 0:
            record.update(state="MERGE-COMMAND-FAILED", detail=merged.stderr.strip()[:600])
            write_json(d / "merge.json", record)
            return 5, {"result": "merge-failed", **record}

        deadline = time.monotonic() + opts.merge_wait
        while True:
            view = json.loads(
                tools.gh(["pr", "view", str(ctx["pr"]), "--repo", run["repo"], "--json", "state,mergeCommit"], repo).stdout
            )
            if view["state"] == "MERGED":
                break
            if view["state"] == "CLOSED" or time.monotonic() >= deadline:
                record.update(state=f"NOT-MERGED:{view['state']}")
                write_json(d / "merge.json", record)
                return 5, {"result": "merge-not-observed", **record}
            tools.sleep(opts.merge_poll)

        result = _record_merged(run_dir, item_id, repo, tools, run["repo"], record, (view.get("mergeCommit") or {}).get("oid"))
        return 0, {**result, "warnings": ctx["warnings"]}
    finally:
        release_lock(tools, repo, token)
        token_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-merge", description=__doc__.split("\n\n")[0])
    p.add_argument("--run-dir", required=True, type=absolute)
    p.add_argument("--repo-dir", type=absolute, required=True, help="a clone of the target repository with an 'origin' remote")
    p.add_argument("--id", help="the item to merge")
    p.add_argument("--pr", help="pull request number (default: the item's recorded pr field)")
    p.add_argument(
        "--outside",
        type=int,
        metavar="PR",
        help="merge a pull request from outside the program, labeled rollout-ready by a steward (program.json `outside_prs: adopt`), instead of an item",
    )
    p.add_argument("--repo", help="with --outside: the repository it is in (owner/name), which must be the run's")
    p.add_argument("--dry-run", action="store_true", help="check every gate and stop before the lock")
    p.add_argument("--after-build", action="store_true", help="check a finished build before it is graded")
    p.add_argument("--release-own", action="store_true", help="free any lock this run still holds, then exit")
    p.add_argument("--allow-no-required-checks", action="store_true")
    p.add_argument("--lock-ref", default=LOCK_REF)
    p.add_argument("--lock-wait", type=float, default=LOCK_WAIT_S)
    p.add_argument("--lock-poll", type=float, default=LOCK_POLL_S)
    p.add_argument("--merge-wait", type=float, default=MERGE_WAIT_S)
    p.add_argument("--merge-poll", type=float, default=MERGE_POLL_S)
    p.add_argument("--root", type=absolute, help="the program's loop root: use its pinned git and gh, after checking both are unchanged")
    p.add_argument("--git", help="absolute path of the git binary (instead of --root)")
    p.add_argument("--gh", help="absolute path of the gh binary (instead of --root)")
    return p


def _root_of(root, run_dir) -> Path | None:
    """The program a run belongs to: the one named, or the one its directory lies in (`<root>/runs/<run>`)."""
    root = Path(root) if root else Path(run_dir).resolve().parent.parent
    return root if (root / "program.json").exists() else None


def _train_ahead(root, run_dir, run: dict) -> tuple[int, int]:
    """How far the run's train is ahead of its last promotion, and its limit."""
    from . import program, train  # here, not at the top: train.py is built on this module

    root = _root_of(root, run_dir)
    if root is None:
        raise Refusal(f"{run['run_id']} merges into a train, and its program was not found: pass --root")
    limits = program.train_settings(program.load(root), run["repo"]) or program.TRAIN_DEFAULTS
    return len(train.unpromoted(root, run["repo"])), int(limits["limit"])


def _repo_facts(root, run_dir) -> dict:
    """What program.json says about the run's repository. Without --root the
    program is the one the run directory lies in (`<root>/runs/<run>`), so
    leaving the option out does not leave the protected paths unread."""
    from . import program  # here, not at the top: only the command line reads settings

    root = _root_of(root, run_dir)
    if root is None:
        return {}
    return program.repo_facts(program.load(root), items.load_run(run_dir)["repo"])


def _facts_if_known(root, run_dir) -> dict | None:
    """What program.json says about the run's repository, or None when the
    program cannot be found. The difference matters where a missing fact
    would pass for "nothing is protected here"."""
    return _repo_facts(root, run_dir) if _root_of(root, run_dir) is not None else None


def require_loop_token(root=None) -> None:
    """On the mini git and gh fall back to the keyring's token, which can
    administer the organization. Nothing here runs on that.

    A token already in the environment is used as it is. Without one, and
    with the root of a program whose settings say `"secrets": "keeper"`, the
    loop's own token is taken from the program's keeper and put in this
    process's environment, which is as far as it goes: it is never printed
    or written. Call this before `Tools` is built, which copies the
    environment."""
    if os.environ.get("GH_TOKEN"):
        return
    if root is not None and (Path(root) / "program.json").exists():
        from . import program, secrets as keeper  # here, not at the top: only the command line reads settings

        try:
            kept = keeper.in_use(program.load(root))
        except (Refusal, OSError, ValueError) as broken:
            # Where the token comes from is one of the settings. Without them the answer is the one it always was.
            raise Refusal(f"GH_TOKEN is not set, and the program's settings could not be read to see whether its keeper gives it: {broken}") from broken
        if kept:
            try:
                os.environ["GH_TOKEN"] = keeper.get(root, "gh")
            except Refusal as refusal:
                raise Refusal(f"GH_TOKEN is not set, and the program's keeper did not give the loop's token: {refusal}") from refusal
            return
    raise Refusal(
        "GH_TOKEN is not set: run this through `hl-secrets --root <root> run -- <command>`, or name the root of a "
        'program whose program.json says `"secrets": "keeper"` (ops card, the top)'
    )


def main(argv: list[str] | None = None) -> int:
    opts = _parser().parse_args(argv)
    try:
        # Without --root the program is the one the run directory lies in, as for the repository's facts.
        require_loop_token(_root_of(opts.root, opts.run_dir))
        if opts.root and (opts.git or opts.gh):
            raise Refusal("pass --root, or --git and --gh, not both")
        if opts.root:
            pinned = claim.check_pins(opts.root, ("git", "gh"))["bins"]
            opts.git, opts.gh = pinned["git"], pinned["gh"]
        tools = Tools(git=opts.git, gh=opts.gh)
        if opts.release_own:
            from . import outside  # here, not at the top: outside.py is built on this module

            # At a session's start nothing of this run is merging: what its items and its outside merges left is freed.
            freed = release_own(tools, opts.repo_dir, opts.run_dir, ref=opts.lock_ref)
            try:
                freed += outside.release_dead(tools, opts.repo_dir, opts.run_dir, ref=opts.lock_ref, only_dead=True)
            except (Refusal, RuntimeError, OSError, ValueError) as failed:
                # What was freed is said whatever happens to the rest.
                emit({"released": freed, "outside_not_released": str(failed)[:300]})
                return 2
            emit({"released": freed})
            return 0
        if opts.outside is not None:
            if opts.id or opts.after_build or opts.pr:
                raise Refusal("--outside merges a pull request from outside the program: not with --id, --pr or --after-build")
            from . import outside  # here, not at the top: outside.py is built on this module

            root = _root_of(opts.root, opts.run_dir)
            if root is None:
                raise Refusal("the program this run belongs to was not found: pass --root")
            code, result = outside.merge_outside(root, opts.run_dir, opts.repo_dir, opts.outside, tools, opts)
            emit(result)
            return code
        if opts.repo:
            raise Refusal("--repo goes with --outside")
        if not opts.id:
            raise Refusal("--id is required")
        if opts.after_build:
            try:
                emit(after_build(opts.run_dir, opts.id, opts.repo_dir, tools, pr=opts.pr, repo_facts=_repo_facts(opts.root, opts.run_dir)))
                return 0
            except NotFinished as not_finished:
                emit(send_back(opts.run_dir, opts.id, not_finished.reasons))
                print(f"hl-merge: build sent back: {not_finished}", file=sys.stderr)
                return 2
        code, result = merge(opts.run_dir, opts.id, opts.repo_dir, tools, opts)
    except Refusal as refusal:
        emit({"result": "refused", "refused": refusal.reasons})
        print(f"hl-merge: refused: {refusal}", file=sys.stderr)
        return 2
    emit(result)
    return code


if __name__ == "__main__":
    sys.exit(main())
