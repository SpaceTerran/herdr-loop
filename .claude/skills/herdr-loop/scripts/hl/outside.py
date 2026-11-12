"""Pull requests from outside the program, rolled out with its own merges.

Steward bots on this host (say, one that keeps the host's services patched
and one that fixes security findings, both run by an assistant bot on the
same host) open fix pull requests in repositories a loop may be working. They never merge there while the loop's manager is alive, so as
never to land a deploy under one of the loop's own. Once a steward's research
says a fix is safe to ship and its required checks are green on an up-to-date
head, it labels the pull request `rollout-ready` and comments the facts,
with one line that names the commit it vetted:

    rollout-ready-head: <the 40-character sha of the head it vetted>

That label and that line are the whole message between them: neither side
prompts the other, and each checks the other's answer at the end of its own
cycle. Without this a ready fix waits until the loop finishes, which can be
days.

    ready      the open `rollout-ready` pull requests in the program's
               repositories and what GitHub says of each: facts only.
               `hl-flow save` adds them to its answer, so a manager sees them
               at the end of every cycle; `hl-handoff list` gives the same.
    merge      `hl-merge --outside <pr>`: one of them merged under the
               repository's merge lock and every gate that applies to a pull
               request nobody here graded, when program.json says
               `"outside_prs": "adopt"`. The head merged is the head the
               steward vetted, or that head with only the base merged into it.
    verify     `hl-verify --outside <pr>`: its deploy, read from the
               repository's own deploy runs (`deploys` in the spec's facts
               for the repository) on the merge commit, or on a later commit
               of the base that carries it; never a command taken from the
               pull request or its comments
    decline    `hl-handoff decline`: passed over for now, with the manager's
               reason, as the label `rollout-held` and a comment

Whether and when to fold a ready pull request into the program's own rollout
stays the manager's decision. These scripts only say what is true and refuse
what is not allowed.

    <run>/outside/<pr>/intent.json  written before the merge; gone once it is recorded
    <run>/outside/<pr>/merge.json   what was merged, from which run, at which commit
    <run>/outside/<pr>/verify.json  what its deploy check found

Until an outside merge's deploy check has settled, nothing else of the
program merges into that repository: not an item, not another outside pull
request. An intent with no record (a merge that died, or one GitHub had not
finished) holds the repository the same way, since the pull request may
have landed.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import items, program, review
from .common import Refusal, read_json, utc_now, write_json

LABEL_READY = "rollout-ready"
LABEL_HELD = "rollout-held"
HELD_COLOR = "d93f0b"
HELD_DESCRIPTION = "A loop passed this over for now; its reason is in a comment"
# The branches the stewards and Renovate open their pull requests from. A pull request from any other branch is
# somebody's own work, and is not rolled out on a label.
BRANCH_PREFIXES = ("secfix/", "steward/", "renovate/")
# The line a steward's comment carries to say which commit it vetted. The label alone outlives a force-push.
VETTED_RE = re.compile(r"^\s*rollout-ready-head:\s*([0-9a-f]{40})\s*$", re.MULTILINE)
SETTING = "outside_prs"
SETTING_VALUES = ("off", "adopt")
# The GitHub logins whose `rollout-ready` label and `rollout-ready-head:` line count: the stewards'. The label and a
# comment are open to anyone who can write to the repository, so who wrote them is what makes them a steward's.
VETTERS = "outside_pr_vetters"
# Whether a vetter login may be the very login the loop's workers act as. Then nothing tells a steward's label from
# one a worker put on its own pull request, so the gate refuses it unless the operator accepts that, here.
SHARED_LOGIN = "outside_pr_shared_login"
SHARED_LOGIN_VALUES = ("refuse", "accept")
SETTLED = ("verified", "no-deploy", "no-run")
LIST_FIELDS = "number,url,title,headRefName,baseRefName,isDraft,mergeable,mergeStateStatus"
VIEW_EXTRA = ",labels,mergeable,isCrossRepository"
# One read of GitHub, and the whole list a save reads: a save is never held long by it.
GH_TIMEOUT_S = 20
READ_BUDGET_S = 45
DEPLOY_WAIT_S = 90 * 60
# The least a deploy check may wait from the merge: before it GitHub may not yet have started the push's runs,
# and a check that settled as `no-run` then would let the next merge land on a deploy just starting.
DEPLOY_WAIT_MIN_S = 10 * 60
DEPLOY_INTERVAL_S = 60
MERGE_COMMIT_LOOKS = 6
# The lock commit names an outside merge's run as `outside:<run>`, so an item's recovery in the same run (which
# frees any lock naming its run) never frees an outside merge in flight, and the other way round.
LOCK_RUN_PREFIX = "outside:"


# ---------------------------------------------------------------- the setting


def setting(cfg: dict) -> str:
    """What program.json says, `off` when it says nothing: a program staged
    before this setting existed adopts nothing."""
    return cfg.get(SETTING) or "off"


def setting_problem(value) -> str | None:
    if value is None or value in SETTING_VALUES:
        return None
    return f"`{SETTING}` must be one of {', '.join(SETTING_VALUES)}, or left out (`off`); got {value!r}"


def vetters(cfg: dict) -> list[str]:
    return [str(v).strip().lower() for v in (cfg.get(VETTERS) or []) if str(v).strip()]


def shared_login_problem(value) -> str | None:
    if value is None or value in SHARED_LOGIN_VALUES:
        return None
    return f"`{SHARED_LOGIN}` must be one of {', '.join(SHARED_LOGIN_VALUES)}, or left out (`refuse`); got {value!r}"


def vetters_problem(value) -> str | None:
    if value is None or (isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)):
        return None
    return f"`{VETTERS}` must be a list of GitHub logins (the stewards'), or left out; got {value!r}"


def adopts(cfg: dict) -> tuple[bool, str | None]:
    """Whether this program may merge an outside pull request now, and if not, why."""
    if setting(cfg) != "adopt":
        return False, f"`{SETTING}` is {setting(cfg)!r} in program.json"
    if not vetters(cfg):
        return False, f"program.json names no steward logins (`{VETTERS}`), so no label or vetted-head line can be told from anyone else's"
    return True, None


def repos_of(cfg: dict) -> list[str]:
    """Every repository the program works, from its spec's facts and its streams, each named once."""
    seen: dict[str, str] = {}
    for name in [*(cfg.get("repos") or {}), *(s.get("repo") for s in (cfg.get("streams") or {}).values() if isinstance(s, dict))]:
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return sorted(seen.values(), key=str.lower)


def branch_allowed(branch: str | None) -> bool:
    return bool(branch) and branch.startswith(BRANCH_PREFIXES)


def _run_under_root(root, run_dir) -> None:
    """Records live where every reader looks: `<root>/runs/<run>`."""
    if Path(run_dir).resolve().parent != (Path(root) / "runs").resolve():
        raise Refusal(f"{run_dir} is not a run of the program at {root} (`<root>/runs/<run>`): its records would be read by nothing")


# ---------------------------------------------------------------- what is ready


class Gh:
    """`gh` with a bound on every call, for the reads that must never hold a save."""

    def __init__(self, gh_bin: str, env: dict | None = None, timeout: float = GH_TIMEOUT_S):
        self.gh_bin, self.env, self.timeout = gh_bin, dict(os.environ if env is None else env), timeout

    def __call__(self, args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        timeout = self.timeout if timeout is None else max(min(timeout, self.timeout), 1)
        try:
            return subprocess.run([self.gh_bin, *args], capture_output=True, text=True, env=self.env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess([self.gh_bin, *args], 124, "", f"gh did not answer within {int(timeout)} s")
        except OSError as exc:
            return subprocess.CompletedProcess([self.gh_bin, *args], 127, "", f"gh could not be run: {exc}")


def checks_summary(proc: subprocess.CompletedProcess) -> str:
    """What `gh pr checks --required` says, in one word and the names that matter."""
    text = (proc.stdout or "").strip()
    said = (proc.stderr or "").lower()
    if not text:
        if "no required checks" in said or "no checks" in said:
            return "none required"
        return f"unread: {(proc.stderr or '').strip()[:160]}"
    try:
        checks = json.loads(text)
    except ValueError:
        return f"unread: {text[:160]}"
    red = sorted(c.get("name", "?") for c in checks if c.get("bucket") in ("fail", "cancel"))
    waiting = sorted(c.get("name", "?") for c in checks if c.get("bucket") not in ("pass", "skipping", "fail", "cancel"))
    if red:
        return "red: " + ", ".join(red)
    if waiting:
        return "pending: " + ", ".join(waiting)
    return "green" if checks else "none required"


def ready(cfg: dict, gh, budget_s: float = READ_BUDGET_S, clock=time.monotonic) -> dict:
    """The open pull requests labeled `rollout-ready` in the program's
    repositories, each as GitHub has it now. Facts only: a repository that
    could not be read is said under `errors`, and nothing here fails. The
    read stops at `budget_s` and says which repositories it did not reach."""
    adopt, why_not = adopts(cfg)
    out: dict = {"adopt": adopt, "label": LABEL_READY, "prs": [], "errors": []}
    if why_not:
        out["adopt_off_because"] = why_not
    deadline = clock() + budget_s

    def call(args):
        # One deadline for the whole read: each call gets what is left of it.
        try:
            return gh(args, timeout=deadline - clock())
        except TypeError:
            return gh(args)

    repos = repos_of(cfg)
    for i, repo in enumerate(repos):
        if clock() >= deadline:
            out["errors"].append(f"not read within {int(budget_s)} s: {', '.join(repos[i:])}")
            break
        listed = call(["pr", "list", "--repo", repo, "--label", LABEL_READY, "--state", "open", "--json", LIST_FIELDS])
        if listed.returncode != 0:
            out["errors"].append(f"{repo}: could not list pull requests: {(listed.stderr or '').strip()[:200]}")
            continue
        try:
            found = json.loads(listed.stdout or "[]")
        except ValueError:
            out["errors"].append(f"{repo}: gh gave no JSON: {(listed.stdout or '')[:120]}")
            continue
        for pr in found:
            number = pr.get("number")
            state = pr.get("mergeStateStatus")
            entry = {
                "repo": repo,
                "pr": number,
                "url": pr.get("url"),
                "title": pr.get("title"),
                "branch": pr.get("headRefName"),
                "base": pr.get("baseRefName"),
                "draft": bool(pr.get("isDraft")),
                "mergeable": pr.get("mergeable"),
                "merge_state": state,
                "behind": state == "BEHIND",
                # `hl-merge --outside` adopts only a pull request from a steward's or Renovate's branch.
                "branch_allowed": branch_allowed(pr.get("headRefName")),
            }
            if clock() >= deadline:
                entry["checks"] = "not read: out of time"
            else:
                entry["checks"] = checks_summary(call(["pr", "checks", str(number), "--repo", repo, "--required", "--json", "name,bucket"]))
            out["prs"].append(entry)
    return out


def ready_or_said(root, no_token: str | None = None, gh=None) -> dict:
    """`ready` for `hl-flow save`, which must never fail on it: whatever goes
    wrong is said in the answer instead."""
    try:
        cfg = program.load(root)
    except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
        reasons = exc.reasons if isinstance(exc, Refusal) else [str(exc)]
        return {"errors": [f"program.json could not be read: {'; '.join(reasons)}"[:300]]}
    adopt, why_not = adopts(cfg)
    if not adopt:
        # A program that does not adopt leaves the stewards' pull requests alone, so nothing about them is read.
        return {"adopt": False, "adopt_off_because": why_not, "label": LABEL_READY, "prs": []}
    if no_token:
        return {"adopt": True, "label": LABEL_READY, "not_checked": f"not read, because the loop's own token could not be had: {no_token}"[:400]}
    try:
        if gh is None:
            gh = Gh(_gh_bin(root))
        return ready(cfg, gh)
    except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
        return {"adopt": adopts(cfg)[0], "label": LABEL_READY, "errors": [f"could not be read: {exc}"[:300]]}


def _gh_bin(root, wait: bool = False) -> str:
    """The program's pinned gh. For a save's read (`wait` false) the pinned path
    as it stands, with no wait for a binary being replaced: the read is a fact,
    and must not hold the save. Otherwise as every other script takes it."""
    from . import claim  # here, not at the top: claim is only needed on the command line

    if wait:
        return claim.check_pins(root, ("gh",))["bins"]["gh"]
    path = (claim.load_pins(root)["bins"].get("gh") or {}).get("path")
    if not path or not os.access(path, os.X_OK):
        raise Refusal(f"the pinned gh ({path}) is not there to run")
    return path


# ---------------------------------------------------------------- the records


def record_dir(run_dir, pr) -> Path:
    return Path(run_dir) / "outside" / str(int(pr))


def _runs_with_outside(root) -> list[Path]:
    """Only the runs that hold an outside record: a program that never merged one reads nothing here."""
    return sorted(p.parent for p in Path(root).glob("runs/*/outside") if p.is_dir())


def _read(path: Path) -> dict | None:
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        return {"unreadable": f"{path.name}: {exc}"[:200]}
    return data if isinstance(data, dict) else {"unreadable": f"{path.name}: not an object"}


def records(root, repo: str | None = None) -> list[dict]:
    """Every outside pull request this program merged or began to merge, in a
    repository or all. `state` is `MERGED` (recorded), `INTENT` (a merge began
    and has no record: it may have landed) or `UNREADABLE` (its files or its
    run could not be read; counted against every repository)."""
    out = []
    for run_dir in _runs_with_outside(root):
        try:
            run = items.load_run(run_dir)
        except Exception as exc:  # noqa: BLE001 - said, and held against every repository
            out.append({"pr": None, "state": "UNREADABLE", "run_id": run_dir.name, "run_dir": str(run_dir), "repo": None, "detail": f"run.json: {exc}"[:200]})
            continue
        if repo and str(run.get("repo", "")).lower() != repo.lower():
            continue
        for d in sorted(p for p in (run_dir / "outside").iterdir() if p.is_dir()):
            base = {"run_id": run["run_id"], "run_dir": str(run_dir), "repo": run["repo"], "dir": str(d)}
            try:
                pr = int(d.name)
            except ValueError:
                continue
            merged = _read(d / "merge.json") if (d / "merge.json").exists() else None
            intent = _read(d / "intent.json") if (d / "intent.json").exists() else None
            if merged is None and intent is None:
                continue
            if (merged and "unreadable" in merged) or (intent and "unreadable" in intent and merged is None):
                out.append({**base, "pr": pr, "state": "UNREADABLE", "detail": (merged or intent)["unreadable"]})
                continue
            if merged is None:
                out.append({**base, "pr": pr, "state": "INTENT", "head_sha": intent.get("head_sha")})
                continue
            if merged.get("state") not in ("MERGED",):
                out.append({**base, "pr": pr, "state": "UNREADABLE", "detail": f"merge.json says state {merged.get('state')!r}"})
                continue
            verified = _read(d / "verify.json") if (d / "verify.json").exists() else {}
            out.append({**merged, **base, "pr": pr, "verify": verified.get("result") if "unreadable" not in verified else "unreadable"})
    return out


def pending(root, repo: str, skip_pr: int | None = None, fixing: int | None = None) -> list[str]:
    """What holds the repository: outside merges whose deploy check has not
    settled, merges that began and have no record, and records that could not
    be read. Nothing else of the program merges into it until each is cleared:
    the next merge would land on top of a deploy that may be red. `skip_pr`
    leaves out the begun merge of that pull request, for its own re-run.
    `fixing` is the outside pull request an item says it fixes forward
    (`fixes_outside`): that one's failed deploy does not hold the item, which
    is the only way the loop can ship the fix."""
    said = []
    for r in records(root, repo):
        if r["state"] == "UNREADABLE":
            said.append(f"outside records of run {r['run_id']} could not be read ({r['detail']}); read them before anything more merges")
        elif r["state"] == "INTENT":
            if skip_pr is not None and r["pr"] == int(skip_pr):
                continue
            said.append(
                f"outside pull request #{r['pr']} began to merge in run {r['run_id']} and has no record, so it may have landed: "
                f"run `hl-merge --outside {r['pr']}` again (from any run in {r['repo']}) to record it"
            )
        elif r.get("state") == "MERGED" and r.get("verify") not in SETTLED:
            if fixing is not None and r["pr"] == int(fixing) and r.get("verify") == "failed":
                continue
            said.append(f"outside pull request #{r['pr']} (merged by run {r['run_id']}, deploy check {r.get('verify') or 'not run'}): `hl-verify --outside {r['pr']}`")
    return said


def pending_items(root, repo: str, tools=None, repo_dir=None) -> list[str]:
    """The program's own items in the repository that an outside merge must
    not land on: merged with a Verify that has not passed; and, read from
    GitHub, an item in progress whose pull request is merged (a merge that
    died after GitHub's answer, whatever local files a session start has
    cleaned up since) or is armed to merge itself."""
    out = []
    for run_file in sorted(Path(root).glob("runs/*/run.json")):
        run_dir = run_file.parent
        try:
            run = items.load_run(run_dir)
        except Exception as exc:  # noqa: BLE001 - said, and held against the merge
            out.append(f"run {run_dir.name} could not be read ({exc}); read it before anything more merges")
            continue
        if str(run.get("repo", "")).lower() != repo.lower() or items.on_train(run):
            continue
        for item_id in run["order"]:
            try:
                item = items.load_item(run_dir, item_id)
            except Exception as exc:  # noqa: BLE001 - said, and held against the merge
                out.append(f"{run['run_id']}/{item_id} could not be read ({exc}); read it before anything more merges")
                continue
            if item["status"] == "merged":
                if item.get("verify_command") and item["fields"].get("verify") not in items.VERIFY_SETTLED:
                    out.append(f"{run['run_id']}/{item_id} is merged and its Verify is not recorded as passed (verify={item['fields'].get('verify')})")
                continue
            pr = item["fields"].get("pr")
            if item["status"] != "in_progress" or not pr or tools is None:
                continue
            seen = tools.gh(["pr", "view", str(pr), "--repo", run["repo"], "--json", "state,autoMergeRequest"], repo_dir, check=False)
            try:
                shown = json.loads(seen.stdout or "{}") if seen.returncode == 0 else None
            except ValueError:
                shown = None
            if shown is None:
                out.append(f"{run['run_id']}/{item_id}'s pull request #{pr} could not be read ({(seen.stderr or '').strip()[:120]}): it may have landed")
            elif shown.get("state") == "MERGED":
                out.append(f"{run['run_id']}/{item_id}'s pull request #{pr} is merged on GitHub and not recorded: its `hl-merge` records it; run that first")
            elif shown.get("state") == "OPEN" and shown.get("autoMergeRequest"):
                out.append(f"{run['run_id']}/{item_id}'s pull request #{pr} is armed to merge itself")
    return out


def find(root, repo: str, pr) -> dict | None:
    for record in records(root, repo):
        if record["state"] == "MERGED" and record["pr"] == int(pr):
            return record
    return None


def _intents(root, repo: str, pr) -> list[dict]:
    return [r for r in records(root, repo) if r["state"] == "INTENT" and r["pr"] == int(pr)]


def _in_flight(r: dict) -> bool:
    token = Path(r["dir"]) / "merge-lock.json"
    if not token.exists():
        return False
    try:
        return _holder_alive(read_json(token))
    except (OSError, ValueError):
        return False


def _clear_intents(root, repo: str, pr) -> list[dict]:
    """Clear the begun merges of this pull request, but never one whose merge is
    still in flight (its lock token names a live holder): only that merge knows
    whether GitHub took it. Returns what was cleared."""
    cleared = []
    for r in _intents(root, repo, pr):
        if _in_flight(r):
            continue
        (Path(r["dir"]) / "intent.json").unlink(missing_ok=True)
        cleared.append(r)
    return cleared


# ---------------------------------------------------------------- the merge


def _view(tools, repo_dir, slug: str, pr) -> dict:
    from . import merge

    for _ in range(6):
        data = json.loads(tools.gh(["pr", "view", str(pr), "--repo", slug, "--json", merge.PR_FIELDS + VIEW_EXTRA], repo_dir).stdout)
        if data.get("mergeStateStatus") != "UNKNOWN" or data.get("state") != "OPEN":
            return data
        tools.sleep(5)
    return data


def _labels(view: dict) -> list[str]:
    return [(label.get("name") if isinstance(label, dict) else str(label)) for label in (view.get("labels") or [])]


def vetted_head(tools, repo_dir, slug: str, pr, allowed: list[str]) -> tuple[str | None, str | None]:
    """The commit a steward last said it vetted, and why none counts when none
    does. Only the newest comment by one of the stewards' logins that names a
    commit counts: if anyone edited it since (any writer can edit a comment,
    and its author stays the same), nothing counts, and an older one never
    comes back in its place. A force-push of the branch after it voids it too."""
    proc = tools.gh(["api", f"repos/{slug}/issues/{int(pr)}/comments", "--paginate"], repo_dir, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"the pull request's comments could not be read: {proc.stderr.strip()[:200]}")
    newest = None
    for comment in json.loads(proc.stdout or "[]"):
        author = ((comment.get("user") or {}).get("login") or "").lower()
        if author in allowed and VETTED_RE.search(comment.get("body") or ""):
            newest = comment
    if newest is None:
        return None, None
    if newest.get("updated_at") != newest.get("created_at"):
        return None, f"the steward's newest `rollout-ready-head:` comment was edited after it was written ({newest.get('updated_at')}): it no longer counts, and the steward vets again"
    pushed = [e for e in _events(tools, repo_dir, slug, pr) if e.get("event") == "head_ref_force_pushed" and (e.get("created_at") or "") > (newest.get("created_at") or "")]
    if pushed:
        return None, f"the branch was force-pushed at {pushed[-1].get('created_at')}, after the steward vetted it: the steward vets again"
    return VETTED_RE.findall(newest.get("body") or "")[-1], None


def _events(tools, repo_dir, slug: str, pr) -> list[dict]:
    proc = tools.gh(["api", f"repos/{slug}/issues/{int(pr)}/events", "--paginate"], repo_dir, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"the pull request's events could not be read: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout or "[]")


def loop_login(tools, repo_dir) -> str | None:
    """The login the loop's own token acts as: its workers hold the same token."""
    proc = tools.gh(["api", "user", "--jq", ".login"], repo_dir, check=False)
    return proc.stdout.strip().lower() or None if proc.returncode == 0 else None


def labeled_by(tools, repo_dir, slug: str, pr) -> str | None:
    """Who last put `rollout-ready` on the pull request, from its own events."""
    actor = None
    for event in _events(tools, repo_dir, slug, pr):
        if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == LABEL_READY:
            actor = ((event.get("actor") or {}).get("login") or "").lower()
    return actor


def _vetted_problems(tools, repo_dir, view: dict, vetted: str | None) -> tuple[list[str], list[str]]:
    """Whether the head about to merge is the one the steward vetted. A head
    that only took the base in since (`gh pr update-branch`, the step a
    pull request that is behind needs) carries the same change: allowed, and
    said. Anything else was pushed after the steward looked."""
    from . import merge

    head = view["headRefOid"]
    if not vetted:
        return [f"pull request #{view['number']} has no `rollout-ready-head: <sha>` line from a steward's login: the label alone does not say which commit was vetted"], []
    if vetted == head:
        return [], []
    base_ref = f"origin/{view['baseRefName']}"
    ancestor = tools.git(["merge-base", "--is-ancestor", vetted, head], repo_dir, check=False).returncode == 0
    if ancestor:
        # Every commit since the vetted one that is not a merge must already be on the base: anything else was pushed
        # to the branch after the steward looked, whatever tree it ends on.
        added = tools.git(["rev-list", "--no-merges", f"{vetted}..{head}", f"^{base_ref}"], repo_dir, check=False)
        if added.returncode != 0 or added.stdout.strip():
            return [f"head is {head[:12]} but the steward vetted {vetted[:12]}, and commits not on the base were pushed since: the steward marks it ready again once it has looked"], []
        try:
            same = merge._patch_id(tools, repo_dir, base_ref, head) == merge._patch_id(tools, repo_dir, base_ref, vetted)
        except RuntimeError:
            same = False
        if same:
            return [], [f"head moved from the vetted {vetted[:12]} to {head[:12]} by taking in the base only; the change is the same"]
    return [f"head is {head[:12]} but the steward vetted {vetted[:12]}, and the change differs: the steward marks it ready again once it has looked"], []


def gate(cfg: dict, root, run_dir, repo_dir, pr, tools, view: dict | None = None) -> dict:
    """Every condition an outside pull request's merge needs, checked together.
    Raises Refusal naming all that fail; returns what the merge needs.

    Nobody here graded it, so what stands in for a review is the steward's:
    its label, its line naming the commit it vetted, on a branch only a
    steward or Renovate opens, in this repository and not a fork. Everything
    else an item is held to that does not come from a review holds here too:
    the pull request's own state, attribution in its text, commits and diff,
    the paths the spec protects (no exemptions: an exemption is an item's),
    the repository's required checks on an up-to-date head (and at least one
    of them), and no merge on top of a deploy check that has not passed."""
    from . import merge

    run = items.load_run(run_dir)
    slug = run["repo"]
    base = cfg.get("base") or "main"
    view = view or _view(tools, repo_dir, slug, pr)
    reasons: list[str] = []
    warnings: list[str] = []
    labels = _labels(view)
    if LABEL_READY not in labels:
        reasons.append(f"pull request #{view['number']} does not carry the label `{LABEL_READY}`: only a pull request a steward marked ready is adopted")
    if LABEL_HELD in labels:
        reasons.append(f"pull request #{view['number']} carries `{LABEL_HELD}`: it was passed over, and stays so until a steward marks it ready again")
    if view.get("isCrossRepository"):
        reasons.append(f"pull request #{view['number']} is from a fork: only a branch in {slug} itself is adopted")
    if not branch_allowed(view.get("headRefName")):
        reasons.append(f"pull request #{view['number']} is from `{view.get('headRefName')}`; only a branch starting {', '.join(BRANCH_PREFIXES)} is adopted")
    if view.get("baseRefName") != base:
        reasons.append(f"pull request #{view['number']} is into `{view.get('baseRefName')}`, not this program's base `{base}`")
    if view.get("isCrossRepository"):
        raise Refusal(reasons)
    fetch_problem = merge._fetch_pr(tools, repo_dir, view)
    if fetch_problem:
        raise Refusal(reasons + [fetch_problem])
    reasons += merge._pr_problems(tools, repo_dir, view, view.get("headRefName"))
    allowed = vetters(cfg)
    shared_accepted = False
    own = loop_login(tools, repo_dir)
    if own is None:
        reasons.append("the login the loop's token acts as could not be read (`gh api user`): the stewards' logins cannot be told from it")
    elif own in allowed and (cfg.get(SHARED_LOGIN) or "refuse") == "accept":
        shared_accepted = True
        warnings.append(f"the loop's own token acts as {own}, a steward's login: program.json accepts that a worker could have marked this ready")
    elif own in allowed:
        reasons.append(
            f"the loop's own token acts as {own}, which is also a steward's login in `{VETTERS}`: any worker holding that token could mark "
            f"its own pull request ready. Give the stewards a login of their own (a GitHub App), or accept the risk with "
            f"`\"{SHARED_LOGIN}\": \"accept\"` in program.json"
        )
    if LABEL_READY in labels:
        actor = labeled_by(tools, repo_dir, slug, pr)
        if actor not in allowed:
            reasons.append(f"`{LABEL_READY}` was last put on by {actor or 'nobody GitHub names'}, not one of the stewards' logins ({', '.join(allowed)})")
    vetted, void = vetted_head(tools, repo_dir, slug, pr, allowed)
    vetted_reasons, vetted_warnings = ([void], []) if void else _vetted_problems(tools, repo_dir, view, vetted)
    reasons += vetted_reasons
    warnings += vetted_warnings

    head = view["headRefOid"]
    facts = program.repo_facts(cfg, slug)
    if "deploys" not in facts:
        reasons.append(f"the spec does not say what deploys {slug} (`deploys` in its facts, `[]` if a merge deploys nothing): an outside merge's deploy could not be checked")
    else:
        missing = _missing_workflows(tools, repo_dir, base, facts.get("deploys") or [])
        if missing:
            reasons.append(f"`deploys` names {', '.join(missing)}, which origin/{base} of {slug} does not have under .github/workflows: its deploy could never be checked")
    protected = facts.get("protected")
    if protected:
        try:
            touched = review.diff_facts(tools, repo_dir, f"origin/{view['baseRefName']}", head, {"protected": protected})["protected"]
        except RuntimeError as exc:
            reasons.append(f"could not read which paths the pull request changes: {exc}")
        else:
            if touched:
                reasons.append(f"the pull request changes paths the spec protects, which no outside pull request may: {', '.join(touched)}")
    for what in pending_items(root, slug, tools, repo_dir):
        reasons.append(f"{what}: nothing merges into {slug} on top of it")
    for what in pending(root, slug, skip_pr=pr):
        reasons.append(f"{what}: nothing more merges into {slug} until then")

    state = view.get("mergeStateStatus")
    checks = merge.required_checks(tools, repo_dir, slug, pr, view.get("baseRefName"))
    not_green = [f"{c['name']}={c.get('bucket') or c.get('state')}" for c in checks if c.get("bucket") not in ("pass", "skipping")]
    unexplained = not view.get("isDraft") and not not_green
    if state == "BLOCKED" and unexplained:
        state = merge.settled_merge_state(tools, repo_dir, slug, pr)
    needs_update = state == "BEHIND"
    if state == "DIRTY" or view.get("mergeable") == "CONFLICTING":
        reasons.append("the pull request conflicts with its base: the steward that opened it resolves that, not the loop")
    elif state not in merge.MERGEABLE_STATES and not needs_update:
        warnings.append(f"merge state is {state}")
    if not checks:
        # The label is a steward's word on a change nobody here graded; the repository's own checks are the rest.
        reasons.append("no required checks are reported for this pull request: an outside pull request merges only on the repository's required checks")
    if not_green:
        reasons.append("required checks are not green: " + ", ".join(sorted(not_green)))
    elif state == "BLOCKED" and unexplained:
        reasons.append(merge.blocked_reason("the pull request", checks))

    subject = f"{view.get('title') or 'Outside pull request'} (#{view['number']})"
    body = f"Outside pull request #{view['number']} from {view.get('headRefName')}, labeled {LABEL_READY}, rolled out by the {cfg['program']} loop (run {run['run_id']}).\n"
    found = merge._attribution("the squash message", subject + "\n" + body)
    if found:
        reasons.append(found)
    if reasons:
        raise Refusal(reasons)
    return {
        "pr": view["number"],
        "url": view.get("url"),
        "repo": slug,
        "branch": view.get("headRefName"),
        "title": view.get("title"),
        "head_sha": head,
        "merge_state": state,
        "needs_update": needs_update,
        "subject": subject,
        "body": body,
        "warnings": warnings,
        "shared_login_accepted": shared_accepted,
    }


def _missing_workflows(tools, repo_dir, base: str, deploys: list[str]) -> list[str]:
    return [w for w in deploys if tools.git(["cat-file", "-e", f"origin/{base}:.github/workflows/{w}"], repo_dir, check=False).returncode != 0]


def _refuse_unadoptable(cfg: dict, root, run_dir, run: dict, opts) -> None:
    adopt, why_not = adopts(cfg)
    if not adopt:
        raise Refusal(f"this program does not adopt outside pull requests ({why_not}): leave them to their stewards")
    repo = getattr(opts, "repo", None)
    if repo and repo.lower() != str(run["repo"]).lower():
        raise Refusal(f"--repo {repo} is not this run's repository ({run['repo']}): pass the run directory of a run in {repo}")
    if getattr(opts, "allow_no_required_checks", False):
        raise Refusal("--allow-no-required-checks is an item's: an outside pull request merges only on the repository's required checks")
    if items.on_train(run) or program.train_settings(cfg, run["repo"]) is not None:
        raise Refusal(
            f"{run['repo']} runs a train: an outside pull request is not put on a batch by this kit, and merging it into the base "
            "would land beside the train's tested batches. Pass it over with `hl-handoff decline` and say so"
        )


def lock_run_id(run: dict) -> str:
    return f"{LOCK_RUN_PREFIX}{run['run_id']}"


def _merge_commit(tools, repo_dir, slug: str, pr, seen: dict) -> str | None:
    """The merge commit GitHub names, asked again a few times while it names none yet."""
    for look in range(MERGE_COMMIT_LOOKS):
        oid = (seen.get("mergeCommit") or {}).get("oid")
        if oid:
            return oid
        if look + 1 < MERGE_COMMIT_LOOKS:
            tools.sleep(5)
            seen = json.loads(tools.gh(["pr", "view", str(pr), "--repo", slug, "--json", "state,mergeCommit"], repo_dir).stdout)
    return None


def _record(root, d: Path, ctx: dict, run: dict, merge_sha: str, tools, repo_dir, recovered: bool = False) -> dict:
    from . import merge

    record = {
        "pr": ctx["pr"],
        "url": ctx.get("url"),
        "repo": run["repo"],
        "branch": ctx.get("branch"),
        "title": ctx.get("title"),
        "head_sha": ctx["head_sha"],
        "run": run["run_id"],
        "state": "MERGED",
        "merge_sha": merge_sha,
        "at": utc_now(),
    }
    if recovered:
        record["recovered"] = True
    if ctx.get("shared_login_accepted"):
        record["shared_login_accepted"] = True
    landed = tools.gh(["api", f"repos/{run['repo']}/commits/{merge_sha}", "--jq", ".commit.message"], repo_dir, check=False)
    record["attribution_on_base"] = merge._attribution("the merged commit", landed.stdout) if landed.returncode == 0 else None
    write_json(d / "merge.json", record)
    # Every begun merge of this pull request is now this record: its own intent, and any another run left.
    (d / "intent.json").unlink(missing_ok=True)
    _clear_intents(root, run["repo"], ctx["pr"])
    return {"result": "merged", "outside": True, **record}


def merge_outside(root, run_dir, repo_dir, pr, tools, opts) -> tuple[int, dict]:
    """`hl-merge --outside`: the gate, the lock, the gate again, the squash of
    exactly the head the gate read, and the record. Exit codes as hl-merge's."""
    from . import merge

    cfg = program.load(root)
    _run_under_root(root, run_dir)
    run = items.load_run(run_dir)
    slug = run["repo"]
    pr = int(pr)
    d = record_dir(run_dir, pr)

    # What this program began is settled first, whatever its settings say now: a merge it began may have landed,
    # and only reading the pull request tells.
    view = _view(tools, repo_dir, slug, pr)
    if view["state"] == "MERGED":
        return _merged_already(root, run, view, tools, repo_dir, opts)
    cleared = _settle_unlanded(root, slug, view, apply=not opts.dry_run)
    if view["state"] != "OPEN":
        raise Refusal(f"pull request #{pr} is {view['state']}: nothing to adopt{cleared}")
    _refuse_unadoptable(cfg, root, run_dir, run, opts)
    ctx = gate(cfg, root, run_dir, repo_dir, pr, tools, view=view)
    if ctx["needs_update"]:
        return 4, {"result": "needs-update", "outside": True, **ctx, "next": f"`gh pr update-branch {pr} --repo {slug}`, wait for its required checks, then run this again"}
    if opts.dry_run:
        return 0, {"result": "dry-run-passed", "outside": True, **ctx}

    d.mkdir(parents=True, exist_ok=True)
    try:
        token = merge.acquire_lock(tools, repo_dir, lock_run_id(run), f"outside-{pr}", ref=opts.lock_ref, wait_s=opts.lock_wait, poll_s=opts.lock_poll)
    except merge.LockTimeout as exc:
        return 3, {"result": "lock-timeout", "holder": exc.holder, "detail": str(exc)}
    token_path = d / "merge-lock.json"
    # Who holds it, so a recovery frees it only once its holder is gone.
    write_json(token_path, {**token, **_holder()})
    try:
        view = _view(tools, repo_dir, slug, pr)
        if view["state"] == "MERGED":
            return _merged_already(root, run, view, tools, repo_dir, opts, holding=True)
        # The lock may have taken a while: the settings and everything the gate reads are read again under it.
        cfg = program.load(root)
        _refuse_unadoptable(cfg, root, run_dir, run, opts)
        ctx = gate(cfg, root, run_dir, repo_dir, pr, tools, view=view)
        if ctx["needs_update"]:
            return 4, {"result": "needs-update", "outside": True, **ctx, "next": f"`gh pr update-branch {pr} --repo {slug}`, wait for its required checks, then run this again"}
        # The last look before the merge: the head is pinned by the merge itself, the label and the base are not.
        last = _view(tools, repo_dir, slug, pr)
        moved = []
        if LABEL_READY not in _labels(last) or LABEL_HELD in _labels(last):
            moved.append(f"its labels are now {', '.join(_labels(last)) or 'none'}")
        if last.get("baseRefName") != view.get("baseRefName"):
            moved.append(f"its base is now `{last.get('baseRefName')}`")
        if last.get("headRefOid") != ctx["head_sha"]:
            moved.append(f"its head is now {str(last.get('headRefOid'))[:12]}")
        if last["state"] != "OPEN":
            moved.append(f"it is now {last['state']}")
        if moved:
            raise Refusal(f"pull request #{pr} changed while it was being checked ({'; '.join(moved)}): nothing merged; run this again")
        # Written before the merge and kept until its record is: while it stands, the repository is held.
        write_json(d / "intent.json", {"head_sha": ctx["head_sha"], "run": run["run_id"], "at": utc_now(), "shared_login_accepted": ctx["shared_login_accepted"]})
        argv = ["pr", "merge", str(pr), "--repo", slug, "--squash", "--match-head-commit", ctx["head_sha"], "--subject", ctx["subject"], "--body", ctx["body"]]
        merged = tools.gh(argv, repo_dir, check=False)
        if merged.returncode != 0:
            # gh can fail after GitHub merged (a lost answer): read the pull request before believing it did not land.
            after = _view(tools, repo_dir, slug, pr)
            if after["state"] == "MERGED" and after.get("headRefOid") == ctx["head_sha"]:
                merge_sha = _merge_commit(tools, repo_dir, slug, pr, after)
                if not merge_sha:
                    return 5, {"result": "merge-commit-unknown", "outside": True, "pr": pr, "next": f"GitHub has not named the merge commit yet: run `hl-merge --outside {pr}` again to record it"}
                result = _record(root, d, ctx, run, merge_sha, tools, repo_dir)
                return 0, {**result, "warnings": ctx["warnings"] + [f"gh said the merge failed ({merged.stderr.strip()[:200]}), and GitHub shows it merged"]}
            failed = {"pr": pr, "head_sha": ctx["head_sha"], "state": "MERGE-COMMAND-FAILED", "detail": merged.stderr.strip()[:600], "at": utc_now()}
            write_json(d / "attempt.json", failed)
            # GitHub may still be finishing it: the intent stays, and the next run of this command settles it.
            return 5, {"result": "merge-failed", "outside": True, **failed, "next": f"run `hl-merge --outside {pr}` again: it records the merge if GitHub made it, and clears it if not"}
        deadline = time.monotonic() + opts.merge_wait
        while True:
            seen = json.loads(tools.gh(["pr", "view", str(pr), "--repo", slug, "--json", "state,mergeCommit"], repo_dir).stdout)
            if seen["state"] == "MERGED":
                break
            if seen["state"] == "CLOSED" or time.monotonic() >= deadline:
                # It may still land: the intent stays, and holds the repository until a re-run settles it.
                write_json(d / "attempt.json", {"pr": pr, "head_sha": ctx["head_sha"], "state": f"NOT-MERGED:{seen['state']}", "at": utc_now()})
                return 5, {"result": "merge-not-observed", "outside": True, "pr": pr, "state": f"NOT-MERGED:{seen['state']}", "next": f"run `hl-merge --outside {pr}` again to record or clear it"}
            tools.sleep(opts.merge_poll)
        merge_sha = _merge_commit(tools, repo_dir, slug, pr, seen)
        if not merge_sha:
            return 5, {"result": "merge-commit-unknown", "outside": True, "pr": pr, "next": f"GitHub has not named the merge commit yet: run `hl-merge --outside {pr}` again to record it"}
        result = _record(root, d, ctx, run, merge_sha, tools, repo_dir)
        return 0, {**result, "warnings": ctx["warnings"], "next": f"`hl-verify --root <root> --run-dir <run> --repo-dir <clone> --outside {pr} --wait`"}
    finally:
        merge.release_lock(tools, repo_dir, token)
        token_path.unlink(missing_ok=True)


def _settle_unlanded(root, slug: str, view: dict, apply: bool = True) -> str:
    """A begun merge of a pull request that GitHub shows closed, or open with no
    merge armed, never landed: its intent is cleared, so it no longer holds the
    repository. Says what was cleared (or, without `apply`, what would be)."""
    if view["state"] == "MERGED" or (view["state"] == "OPEN" and view.get("autoMergeRequest")):
        return ""
    stale = [r for r in _intents(root, slug, view["number"]) if not _in_flight(r)]
    if apply:
        stale = _clear_intents(root, slug, view["number"])
    return f"; the begun merge recorded in {', '.join(r['run_id'] for r in stale)} is cleared, since it never landed" if stale else ""


def _merged_already(root, run: dict, view: dict, tools, repo_dir, opts, holding: bool = False) -> tuple[int, dict]:
    """The pull request is merged. Recorded already: say so. Begun by a merge
    of this program that died or did not see GitHub finish, at the head it
    meant to merge: record it now, in the run that began it, and free only
    that merge's own lock. Merged by anyone else: not this program's to record."""
    pr = int(view["number"])
    known = find(root, run["repo"], pr)
    if known:
        result = {"result": "already-recorded", "outside": True, **{k: v for k, v in known.items() if k not in ("run_dir", "dir")}}
        if not holding and not opts.dry_run:
            result["released"] = release_dead(tools, repo_dir, Path(known["run_dir"]), ref=opts.lock_ref, only_pr=pr, only_dead=True)
        return 0, result
    begun = _intents(root, run["repo"], pr)
    if not begun:
        raise Refusal(
            f"pull request #{pr} is merged already, and not by this program's `hl-merge --outside`: nothing to adopt, and nothing "
            "is recorded. If it landed in this program's repository while a deploy check was pending, tell the operator"
        )
    elsewhere = [r for r in begun if r.get("head_sha") != view["headRefOid"]]
    if opts.dry_run:
        return 0, {"result": "already-merged", "outside": True, "pr": pr}
    merge_sha = _merge_commit(tools, repo_dir, run["repo"], pr, view)
    if not merge_sha:
        return 5, {"result": "merge-commit-unknown", "outside": True, "pr": pr, "next": f"GitHub has not named the merge commit yet: run `hl-merge --outside {pr}` again"}
    d = Path(begun[0]["dir"])
    began_in = items.load_run(Path(begun[0]["run_dir"]))
    began = read_json(Path(begun[0]["dir"]) / "intent.json") if (Path(begun[0]["dir"]) / "intent.json").exists() else {}
    ctx = {
        "pr": pr,
        "url": view.get("url"),
        "branch": view.get("headRefName"),
        "title": view.get("title"),
        "head_sha": view["headRefOid"],
        "shared_login_accepted": bool(began.get("shared_login_accepted")),
    }
    result = _record(root, d, ctx, began_in, merge_sha, tools, repo_dir, recovered=True)
    if len(elsewhere) == len(begun):
        # This program began it, and it landed at another head: it is in the repository all the same, so it is
        # recorded and its deploy checked like any other; and the operator hears of it.
        record = read_json(d / "merge.json")
        record["landed_at_another_head"] = {"began": begun[0].get("head_sha"), "landed": view["headRefOid"]}
        write_json(d / "merge.json", record)
        result["warning"] = f"pull request #{pr} landed at {view['headRefOid'][:12]}, not the {str(begun[0].get('head_sha'))[:12]} this program began to merge: tell the operator"
    if not holding:
        result["released"] = release_dead(tools, repo_dir, Path(begun[0]["run_dir"]), ref=opts.lock_ref, only_pr=pr, only_dead=True)
    return 0, result


def _holder(pid: int | None = None) -> dict:
    """Who holds a lock token: the process, when it started, and on which host. A pid alone is reused."""
    from .claim import Procs

    pid = pid or os.getpid()
    return {"pid": pid, "start": Procs().start(pid), "host": socket.gethostname()}


def _holder_alive(token: dict) -> bool:
    """Whether the process that wrote the token is still running: the same pid,
    started at the same time, on this host. A token without a start time (or
    from another host) is never taken for alive."""
    from .claim import Procs

    if token.get("host") != socket.gethostname() or not token.get("pid") or not token.get("start"):
        return False
    try:
        return Procs().start(int(token["pid"])) == token["start"]
    except Exception:  # noqa: BLE001 - a process that cannot be read is not one that holds anything
        return False


def release_dead(tools, repo_dir, run_dir, ref, only_pr: int | None = None, only_dead: bool = False) -> list[str]:
    """Free what an outside merge of this run left: its token files, and a lock
    commit naming this run's outside merges. With `only_pr`, only that pull
    request's; with `only_dead`, only a lock whose holder is no longer alive
    (a recovery mid-session must not free a merge still in flight). An
    item's recovery never calls this, and this never frees an item's lock:
    the two name different runs on the lock (`outside:<run>`)."""
    from . import merge

    if not (Path(run_dir) / "outside").is_dir():
        # No outside merge was ever tried from this run (its directory is made before the lock is taken): nothing to free.
        return []
    run = items.load_run(run_dir)
    freed: list[str] = []
    for token_path in sorted(Path(run_dir).glob("outside/*/merge-lock.json")):
        if only_pr is not None and token_path.parent.name != str(only_pr):
            continue
        token = read_json(token_path)
        if only_dead and _holder_alive(token):
            return freed
        if merge.release_lock(tools, repo_dir, token):
            freed.append(f"{token['item_id']}: released {token['held_sha'][:12]}")
        token_path.unlink()
    sha = merge.lock_remote_sha(tools, repo_dir, ref)
    if sha:
        seen = merge._lock_commit(tools, repo_dir, ref, sha)
        names_run = seen and re.search(rf"^run_id: {re.escape(lock_run_id(run))}$", seen[1], re.MULTILINE)
        names_pr = only_pr is None or (seen and re.search(rf"^item_id: outside-{int(only_pr)}$", seen[1], re.MULTILINE))
        if names_run and names_pr and merge.release_lock(tools, repo_dir, {"ref": ref, "held_sha": sha}):
            freed.append(f"remote lock {sha[:12]} named this run's outside merge: released")
    return freed


# ---------------------------------------------------------------- the deploy check


def _epoch(stamp: str) -> float:
    return datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def _runs(tools, repo_dir, slug: str, workflow: str, *selector: str) -> tuple[list[dict] | None, str | None]:
    proc = tools.gh(
        ["run", "list", "--repo", slug, "--workflow", workflow, *selector, "--json", "databaseId,headSha,status,conclusion,url,event", "--limit", "20"],
        repo_dir,
        check=False,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip()[:200]
    try:
        return json.loads(proc.stdout or "[]"), None
    except ValueError:
        return None, (proc.stdout or "")[:200]


def _word(run: dict) -> str:
    if run.get("status") != "completed":
        return "running"
    if run.get("conclusion") == "success":
        return "passed"
    if run.get("conclusion") in ("skipped", "neutral"):
        return "skipped"
    return "failed"


def _carries(tools, repo_dir, merge_sha: str, sha: str | None) -> bool:
    return bool(sha) and sha != merge_sha and tools.git(["merge-base", "--is-ancestor", merge_sha, sha], repo_dir, check=False).returncode == 0


def _workflow_state(tools, repo_dir, slug: str, base: str, workflow: str, sha: str) -> dict:
    """What one deploy workflow says of the merge. Its own run on the merge
    commit first. Where that is missing or did not pass, a later run on the
    base whose commit carries the merge (a deploy that cancelled ours, or the
    next push's): passed, it deployed this change too (`superseded`); running,
    it is still deploying it; failed, the change is in a red deploy."""
    own, unread = _runs(tools, repo_dir, slug, workflow, "--commit", sha)
    if own is None:
        return {"workflow": workflow, "state": "unread", "detail": unread}
    state = {"workflow": workflow, "state": "none"}
    if own:
        # Every run of the workflow on the commit counts, not only the newest: a skipped run can be newer than the
        # one that deployed. Any still running is running; otherwise the newest run that was not skipped decides.
        running = [r for r in own if _word(r) == "running"]
        real = [r for r in own if _word(r) != "skipped"]
        chosen = running[0] if running else (real[0] if real else own[0])
        state = {"workflow": workflow, "state": _word(chosen), "run": chosen.get("databaseId"), "url": chosen.get("url"), "conclusion": chosen.get("conclusion")}
    if state["state"] not in ("none", "failed"):
        return state
    tools.git(["fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"], repo_dir, check=False)
    later, _ = _runs(tools, repo_dir, slug, workflow, "--branch", base)
    carrying = [r for r in (later or []) if _carries(tools, repo_dir, sha, r.get("headSha"))]
    if not carrying:
        return state
    newest = carrying[0]
    word = _word(newest)
    if word == "passed":
        return {**state, "state": "superseded", "by_run": newest.get("databaseId"), "by_sha": newest.get("headSha"), "url": newest.get("url")}
    if word in ("running", "failed"):
        # A later deploy carrying the merge is still running (often the very one that cancelled ours): wait for it.
        # Or it failed: the change is in a red deploy, and the newest deploy is the one that says so.
        out = {**state, "state": word, "by_run": newest.get("databaseId"), "by_sha": newest.get("headSha"), "url": newest.get("url")}
        if word == "failed":
            out["fact"] = "a later deploy that carries this change failed: never re-run the deploy of an older commit (that rolls production back); fix it forward"
        return out
    return state


def verify_outside(root, run_dir, repo_dir, pr, tools, opts, clock=time.time, sleep=time.sleep) -> tuple[int, dict]:
    """`hl-verify --outside`: read the repository's deploy runs on the merge
    commit until each has passed (or a later deploy that carries the merge
    has) or was skipped, one has failed, or the deadline has passed. The
    deadline counts from the merge, or from a `--retry`. Exit codes as
    hl-verify's: 0 settled, 3 not yet, 4 failed."""
    from .verify import Lock

    cfg = program.load(root)
    _run_under_root(root, run_dir)
    run = items.load_run(run_dir)
    slug = run["repo"]
    base = cfg.get("base") or "main"
    given = getattr(opts, "deploy_wait", None)
    wait_s = DEPLOY_WAIT_S if given is None else float(given)
    if wait_s < DEPLOY_WAIT_MIN_S:
        raise Refusal(f"--deploy-wait {int(wait_s)} is less than {DEPLOY_WAIT_MIN_S} s: GitHub may not have started the merge's deploy runs yet")
    retry = bool(getattr(opts, "retry", False))
    if retry and not getattr(opts, "note", None):
        raise Refusal("--retry needs --note: what was fixed or run again since the failure")
    d = record_dir(run_dir, pr)
    if not (d / "merge.json").exists():
        raise Refusal(f"no outside merge of #{pr} is recorded in this run: `hl-merge --outside {pr}` first")
    record = read_json(d / "merge.json")
    if record.get("state") != "MERGED" or not record.get("merge_sha"):
        raise Refusal(f"outside pull request #{pr} is not recorded as merged at a commit ({record.get('state')}, {record.get('merge_sha')})")
    verify_file = d / "verify.json"
    before = read_json(verify_file) if verify_file.exists() else {}
    if before.get("result") in SETTLED:
        return 0, {"result": before["result"], "outside": True, "pr": int(pr), "recorded": before}
    if before.get("result") == "failed" and not retry:
        return 4, {
            "result": "failed",
            "outside": True,
            "pr": int(pr),
            "recorded": before,
            "next": f"read the failed run; once its cause is fixed or it has been run again, `hl-verify --outside {pr} --retry --note \"<what changed>\"`",
        }
    facts = program.repo_facts(cfg, slug)
    if "deploys" not in facts:
        raise Refusal(f"the spec does not say what deploys {slug} (`deploys` in its facts, `[]` if a merge deploys nothing): there is nothing to read")
    deploys = facts.get("deploys") or []
    if not deploys:
        out = {"result": "no-deploy", "at": utc_now(), "merge_sha": record["merge_sha"], "fact": f"the spec says a merge deploys nothing in {slug} (`deploys: []`)"}
        write_json(verify_file, out)
        return 0, {"outside": True, "pr": int(pr), **out}
    merged_at = _epoch(record["at"])
    tools.git(["fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"], repo_dir, check=False)
    missing = _missing_workflows(tools, repo_dir, base, deploys)
    if missing:
        raise Refusal(f"`deploys` names {', '.join(missing)}, which origin/{base} of {slug} does not have under .github/workflows: correct the spec and stage it")
    with Lock(d, wait_s + 600):
        if retry:
            # A retry is a new attempt with a deadline of its own: the old one may be long past.
            before = {"result": "retrying", "deadline": clock() + wait_s, "note": opts.note, "previous": before, "at": utc_now()}
            write_json(verify_file, before)
        deadline = before.get("deadline") if before.get("result") == "retrying" else merged_at + wait_s
        while True:
            states = [_workflow_state(tools, repo_dir, slug, base, workflow, record["merge_sha"]) for workflow in deploys]
            past = clock() >= deadline
            failed = [s for s in states if s["state"] == "failed"]
            open_ = [s for s in states if s["state"] in ("running", "unread")]
            # A run GitHub skipped is as final as one that passed; only a workflow with no run yet may still start one.
            final = all(s["state"] in ("passed", "superseded", "skipped") for s in states)
            passed = any(s["state"] in ("passed", "superseded") for s in states)
            result = None
            if failed:
                result = "failed"
            elif final or (past and not open_):
                result = "verified" if passed else "no-run"
            elif past:
                result = "failed"
            if result:
                out = {"result": result, "at": utc_now(), "merge_sha": record["merge_sha"], "workflows": states}
                if result == "no-run":
                    out["fact"] = "no deploy workflow ran on the merge commit, or on a later one that carries it, by the deadline (its path filters may have left it out): nothing was deployed"
                elif result == "failed" and not failed:
                    out["fact"] = "a deploy run was still running, or could not be read, at the deadline"
                if before.get("result") == "retrying":
                    out["retry"] = {"note": before.get("note"), "previous": before.get("previous")}
                write_json(verify_file, out)
                return (0 if result in SETTLED else 4), {"outside": True, "pr": int(pr), **out}
            if not getattr(opts, "wait", False):
                return 3, {"result": "not-yet", "outside": True, "pr": int(pr), "workflows": states, "minutes_left": round((deadline - clock()) / 60, 1)}
            sleep(min(getattr(opts, "interval", DEPLOY_INTERVAL_S), max(deadline - clock(), 1)))


# ---------------------------------------------------------------- passing one over


def decline(cfg: dict, repo: str, pr, reason: str, gh, root=None) -> dict:
    """Take `rollout-ready` off, put `rollout-held` on, and say why on the pull
    request, where the steward reads it at the end of its next run. Only a
    pull request that carries `rollout-ready` is passed over here (an item's
    own pull request is never relabeled by this), and only by a program that
    adopts: one that does not leaves the stewards' pull requests alone. A
    merge of it this program began and that never landed is cleared."""
    from . import merge

    adopt, why_not = adopts(cfg)
    if not adopt:
        raise Refusal(f"this program does not adopt outside pull requests ({why_not}): it leaves them to their stewards, and declines none")
    names = {name.lower(): name for name in repos_of(cfg)}
    if repo.lower() not in names:
        raise Refusal(f"{repo} is not one of this program's repositories ({', '.join(names.values()) or 'none'})")
    reason = " ".join((reason or "").split())
    if not reason:
        raise Refusal("--reason is required: the steward acts on it")
    found = merge._attribution("the reason", reason)
    if found:
        raise Refusal(found)
    repo = names[repo.lower()]
    viewed = gh(["pr", "view", str(int(pr)), "--repo", repo, "--json", "labels,state,autoMergeRequest"])
    if viewed.returncode != 0:
        raise Refusal(f"pull request #{pr} in {repo} could not be read: {(viewed.stderr or '').strip()[:200]}")
    try:
        shown = json.loads(viewed.stdout or "{}")
    except ValueError:
        shown = {}
    if not isinstance(shown, dict) or LABEL_READY not in _labels(shown):
        raise Refusal(f"pull request #{pr} in {repo} does not carry `{LABEL_READY}`: only a pull request a steward marked ready is passed over")
    if root is not None and shown.get("state") in ("OPEN", "CLOSED") and not shown.get("autoMergeRequest"):
        _clear_intents(root, repo, pr)
    made = gh(["label", "create", LABEL_HELD, "--repo", repo, "--color", HELD_COLOR, "--description", HELD_DESCRIPTION])
    if made.returncode != 0 and "already exists" not in (made.stderr or "").lower():
        raise Refusal(f"the label {LABEL_HELD} could not be made in {repo}: {(made.stderr or '').strip()[:200]}")
    edited = gh(["pr", "edit", str(int(pr)), "--repo", repo, "--remove-label", LABEL_READY, "--add-label", LABEL_HELD])
    if edited.returncode != 0:
        raise Refusal(f"pull request #{pr} in {repo} could not be relabeled: {(edited.stderr or '').strip()[:200]}")
    body = f"Passed over for now by the {cfg['program']} loop: {reason}\n\nLabel `{LABEL_HELD}` is the loop's answer; the steward that marked this ready reads it at the end of its next run."
    said = gh(["pr", "comment", str(int(pr)), "--repo", repo, "--body", body])
    if said.returncode != 0:
        raise Refusal(f"pull request #{pr} in {repo} was relabeled, but the reason could not be posted: {(said.stderr or '').strip()[:200]}")
    return {"result": "declined", "repo": repo, "pr": int(pr), "reason": reason, "labels": {"removed": LABEL_READY, "added": LABEL_HELD}}


# ---------------------------------------------------------------- hl-handoff


def _parser():
    import argparse

    from .common import absolute

    p = argparse.ArgumentParser(prog="hl-handoff", description="Pull requests from outside the program that a steward marked ready for rollout.")
    p.add_argument("--root", type=absolute, required=True, help="the program's loop root")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help=f"the open `{LABEL_READY}` pull requests in the program's repositories, as GitHub has them now")
    s = sub.add_parser("decline", help=f"pass one over for now: `{LABEL_READY}` off, `{LABEL_HELD}` on, the reason as a comment")
    s.add_argument("--repo", required=True, help="owner/name")
    s.add_argument("--pr", required=True, type=int)
    s.add_argument("--reason", required=True, help="why, for the steward that marked it ready")
    return p


def main(argv: list[str] | None = None, gh=None) -> int:
    import sys

    from . import merge
    from .common import emit

    args = _parser().parse_args(argv)
    try:
        # These read and write GitHub: on the loop's own token, as every other script of the kit.
        merge.require_loop_token(args.root)
        cfg = program.load(args.root)
        gh = gh or Gh(_gh_bin(args.root, wait=True))
        if args.cmd == "list":
            result = ready(cfg, gh)
        else:
            result = decline(cfg, args.repo, args.pr, args.reason, gh, root=args.root)
    except Refusal as refusal:
        emit({"result": "refused", "refused": refusal.reasons})
        print(f"hl-handoff: refused: {refusal}", file=sys.stderr)
        return 2
    emit(result)
    return 0
