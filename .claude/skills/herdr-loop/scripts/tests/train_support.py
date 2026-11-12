"""A stand-in for GitHub that keeps pull requests in a JSON file and makes
their merges in a real bare repository, for the tests of a train: what a
promotion leaves on the base branch is then read from git itself."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hl import items, merge, program, train
from hl.common import read_json, write_json

from .test_merge import git

LIVE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, tempfile, urllib.parse
path = os.environ["HL_FAKE_GH"]
state = json.load(open(path))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
origin = state["origin"]
def done(code=0):
    json.dump(state, open(path, "w"))
    sys.exit(code)
def g(*a, cwd=None):
    p = subprocess.run(["git", *a], cwd=cwd or origin, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()
def flag(name, default=None):
    return args[args.index(name) + 1] if name in args else default
def fields():
    out = {}
    for i, a in enumerate(args):
        if a in ("-f", "-F") and i + 1 < len(args) and "=" in args[i + 1]:
            k, v = args[i + 1].split("=", 1)
            out[k] = v
    return out
def pull(n):
    found = state["prs"].get(str(n))
    if not found:
        sys.stderr.write("no pull request %s\n" % n)
        done(1)
    return found
def head_of(pr):
    if pr["state"] == "OPEN":
        return g("rev-parse", "refs/heads/" + pr["headRefName"])[1] or pr.get("headRefOid")
    return pr.get("headRefOid")
if args[:1] == ["api"]:
    method = flag("--method", "GET")
    target = [a for a in args[1:] if a.startswith("repos/")][0]
    parts = target.split("/")
    if method == "GET" and parts[3] == "branches" and parts[-2:] == ["protection", "required_status_checks"]:
        wanted = state.get("required", {}).get(urllib.parse.unquote(parts[4]), [])
        if not wanted:
            sys.stderr.write("gh: Branch not protected (HTTP 404)\n")
            done(1)
        print(json.dumps(wanted))
        done()
    if method == "GET" and parts[3] == "rules":
        print("[]")
        done()
    if method == "POST" and parts[3:] == ["pulls"]:
        given = fields()
        n = state["next"]
        state["next"] = n + 1
        state["prs"][str(n)] = {"number": n, "url": "https://example.invalid/pull/%d" % n, "state": "OPEN", "isDraft": False, "title": given["title"],
                                "body": given.get("body", ""), "headRefName": given["head"], "baseRefName": given["base"], "autoMergeRequest": None, "mergeCommit": None}
        print(json.dumps({"number": n, "html_url": "https://example.invalid/pull/%d" % n}))
        done()
    if method == "PATCH" and parts[3] == "pulls":
        pr = pull(parts[4])
        given = fields()
        if given.get("state") == "closed" and pr["state"] == "OPEN":
            pr["headRefOid"] = head_of(pr)
            pr["state"] = "CLOSED"
        if given.get("base"):
            pr["baseRefName"] = given["base"]
        done()
    if method == "POST" and parts[3] == "issues":
        print("https://example.invalid/pull/%s#issuecomment-1" % parts[4])
        done()
    if parts[3] == "commits":
        print(g("log", "-1", "--format=%B", parts[4])[1])
        done()
if args[:2] == ["pr", "list"]:
    head = flag("--head")
    print(json.dumps([{"number": p["number"], "url": p["url"]} for p in state["prs"].values() if p["headRefName"] == head and p["state"] == "OPEN"]))
    done()
if args[:2] == ["pr", "view"]:
    pr = pull(args[2])
    shown = dict(pr, headRefOid=head_of(pr), mergeStateStatus=state.get("merge_state", {}).get(str(pr["number"]), "CLEAN"))
    print(json.dumps({k: shown.get(k) for k in flag("--json").split(",")}))
    done()
if args[:2] == ["pr", "checks"]:
    pr = pull(args[2])
    checks = state["checks"].get(str(pr["number"]), state["checks"].get("*", []))
    if "--required" in args:
        wanted = state.get("required", {}).get(pr["baseRefName"], [])
        checks = [c for c in checks if c["name"] in wanted]
        if not wanted:
            sys.stderr.write("no required checks reported on the '%s' branch\n" % pr["baseRefName"])
            done(1)
    if not checks:
        sys.stderr.write("no checks reported on the '%s' branch\n" % pr["headRefName"])
        done(1)
    print(json.dumps(checks))
    done()
if args[:2] == ["pr", "merge"]:
    pr = pull(args[2])
    head = head_of(pr)
    if state.get("merge_rc") or pr["state"] != "OPEN" or flag("--match-head-commit", head) != head:
        sys.stderr.write("GraphQL: Pull request is not mergeable\n")
        done(state.get("merge_rc") or 1)
    with tempfile.TemporaryDirectory() as work:
        ident = ["-c", "user.name=github", "-c", "user.email=github@example.invalid", "-c", "commit.gpgsign=false"]
        g("clone", "-q", origin, work)
        g("checkout", "-q", "-B", "landing", "origin/" + pr["baseRefName"], cwd=work)
        message = ["-m", flag("--subject", pr["title"]), "-m", flag("--body", "")]
        if "--squash" in args:
            code, _, err = g(*ident, "merge", "--squash", head, cwd=work)
            code = code or g(*ident, "commit", *message, cwd=work)[0]
        else:
            code, _, err = g(*ident, "merge", "--no-ff", *message, head, cwd=work)
        if code:
            sys.stderr.write("GraphQL: merge conflict\n")
            done(1)
        g("push", "-q", "origin", "HEAD:refs/heads/" + pr["baseRefName"], cwd=work)
        landed = g("rev-parse", "HEAD", cwd=work)[1]
    pr.update(state="MERGED", headRefOid=head, mergeCommit={"oid": landed})
    if state.get("delete_branch_on_merge", True):
        g("update-ref", "-d", "refs/heads/" + pr["headRefName"])
    done()
sys.stderr.write("live gh: unexpected " + " ".join(args) + "\n")
done(64)
'''

REPO = "owner/repo"
GREEN = [{"name": "app-tests", "bucket": "pass", "state": "SUCCESS", "link": "https://example.invalid/o/r/actions/runs/11/job/1"}]
TEST_REF = "refs/hl-test/merge-lock"


def backlog(*ids, verify: str | None = None, scope: str | None = None) -> str:
    return "\n".join(
        f"## {i} — Item {i}\n\n**Scope.** {scope or f'Do {i}.'}\n\n**Accept.**\n1. It is done.\n" + (f"\n**Verify.** `{verify}`\nWait. 5\n" if verify else "") for i in ids
    )


class TrainCase(unittest.TestCase):
    """A program whose one repository runs a train: a bare origin, the
    stream's clone, and GitHub as the script above. `batch` is 2 and `limit`
    is 4, so a test is due after two merges."""

    TRAIN = "hl-train/canary"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / "root"
        self.origin = self.tmp / "origin.git"
        self.clone = self.root / "repos" / "app" / "repo"
        self.clone.parent.mkdir(parents=True)
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        git(self.tmp, "clone", str(self.origin), str(self.clone))
        self.identify(self.clone)
        (self.clone / "README.md").write_text("base\n")
        git(self.clone, "add", "README.md")
        git(self.clone, "commit", "-m", "base")
        git(self.clone, "push", "origin", "HEAD:main")
        git(self.clone, "fetch", "origin")
        git(self.clone, "checkout", "--detach", "origin/main")
        self.settings()

        self.gh_state = self.tmp / "gh.json"
        self.gh_state.write_text(json.dumps({"origin": str(self.origin), "prs": {}, "next": 1, "checks": {"*": GREEN}, "required": {"main": ["app-tests"]}}))
        gh = self.tmp / "gh"
        gh.write_text(LIVE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        patcher = mock.patch.dict(os.environ, {"GH_TOKEN": "loop-token-for-tests", "HL_FAKE_GH": str(self.gh_state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tools = merge.Tools(gh=str(gh), sleep=lambda s: None)
        self.n = 0

    def identify(self, repo):
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(repo, "config", key, value)

    def settings(self, **changes):
        base = {
            "program": "canary", "base": "main", "worker_claude_ref": "op://x/y/z", "gh_token_ref": "op://x/y/z", "cursor_model": "m",
            "premise": "routed", "review": "code",
            "streams": {"app": {"repo": REPO, "clone": str(self.clone)}},
            "repos": {REPO: {"train": {"batch": 2, "limit": 4}}},
        }  # fmt: skip
        current = read_json(self.root / "program.json") if (self.root / "program.json").exists() else base
        write_json(self.root / "program.json", {**current, **changes})
        return program.load(self.root)

    @property
    def cfg(self):
        return program.load(self.root)

    # ------------------------------------------------------------ GitHub

    def gh(self, **changes):
        state = read_json(self.gh_state)
        state.update(changes)
        self.gh_state.write_text(json.dumps(state))
        return state

    def checks(self, pr, *checks):
        """What a pull request's checks show; `"*"` is every pull request that has none of its own."""
        state = read_json(self.gh_state)
        if pr == "*":
            state["checks"] = {"*": list(checks)}
        else:
            state["checks"][str(pr)] = list(checks)
        self.gh_state.write_text(json.dumps(state))

    def pr(self, n):
        return read_json(self.gh_state)["prs"][str(n)]

    def calls(self, *words):
        return [c for c in read_json(self.gh_state).get("calls", []) if c[: len(words)] == list(words)]

    # ------------------------------------------------------------ git

    def sha(self, ref):
        return git(self.origin, "rev-parse", ref)

    def tree(self, ref):
        return git(self.origin, "rev-parse", f"{ref}^{{tree}}")

    def push(self, branch, name, text, message, start=None, author=None):
        """A commit pushed to a branch of the origin from a scratch clone, by something other than the loop."""
        self.n += 1
        other = self.tmp / f"other-{self.n}"
        git(self.tmp, "clone", "-q", str(self.origin), str(other))
        self.identify(other)
        git(other, "checkout", "-q", "-B", "work", f"origin/{start or branch}")
        (other / name).parent.mkdir(parents=True, exist_ok=True)
        (other / name).write_text(text)
        git(other, "add", name)
        git(other, "commit", "-m", message, *(["--author", author] if author else []))
        git(other, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return git(other, "rev-parse", "HEAD")

    # ------------------------------------------------------------ runs and items

    def run_of(self, name, *ids, verify=None, scope=None, repair=False):
        """A run of the program in the repository, created the way `hl-items init --root` creates one."""
        run_dir = self.root / "runs" / name
        file = self.tmp / f"{name}-ITEMS.md"
        file.write_text(backlog(*ids, verify=verify, scope=scope))
        policy = {"premise": "routed", "review": "code", "bounds": {}, "merge": "train", "train": {"branch": self.TRAIN, "item_checks": []}}
        items.init_run(run_dir, name, REPO, [str(file)], policy=policy, stream="app", repair=repair)
        return run_dir

    def join(self, branch, other, name, text):
        """A branch cut from the train on which `other` is merged, a conflict in `name` resolved to `text`: a real merge commit, pushed."""
        self.n += 1
        work = self.tmp / f"other-{self.n}"
        git(self.tmp, "clone", "-q", str(self.origin), str(work))
        self.identify(work)
        git(work, "checkout", "-q", "-B", "work", f"origin/{self.TRAIN}")
        subprocess.run(["git", "-C", str(work), "merge", "--no-ff", "--no-edit", f"origin/{other}"], capture_output=True)
        (work / name).write_text(text)
        git(work, "add", name)
        git(work, "commit", "--no-edit", "-m", f"merge {other}")
        git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    def land(self, run_dir, item_id, name=None, text=None, joined=None):
        """An item merged into the train as `hl-merge` leaves it: a pull request squashed in, and the merge on record.
        With `joined`, the item's branch is a merge of that branch and its pull request is merged whole."""
        train.ensure(self.cfg, REPO, self.clone, self.tools)
        run = items.load_run(run_dir)
        branch = f"hl/{run['run_id']}/{item_id}"
        if joined:
            self.join(branch, joined, name, text)
        else:
            self.push(branch, name or f"{item_id}.txt", text or f"{item_id}\n", f"{item_id}: work", start=self.TRAIN)
        made = json.loads(self.tools.gh(["api", "--method", "POST", f"repos/{REPO}/pulls", "-f", f"title={item_id}", "-f", f"head={branch}", "-f", f"base={self.TRAIN}", "-f", "body=x"], self.clone).stdout)
        how = "--merge" if joined else "--squash"
        self.tools.gh(["pr", "merge", str(made["number"]), "--repo", REPO, how, "--subject", f"{item_id}: Item {item_id} (#{made['number']})", "--body", "reviewed"], self.clone)
        merged = self.pr(made["number"])["mergeCommit"]["oid"]
        item = items.load_item(run_dir, item_id)
        item["status"] = "merged"
        items.save_item(run_dir, item)
        write_json(items.item_dir(run_dir, item_id) / "merge.json", {"state": "MERGED", "at": "2026-10-12T12:00:00Z", "merge_sha": merged, "pr": made["number"]})
        return merged

    def opts(self, *argv):
        return train._parser().parse_args(["--root", str(self.root), "--repo", REPO, "--lock-ref", TEST_REF, "--lock-wait", "0.3", "--lock-poll", "0.05", "--merge-wait", "0.2", "--merge-poll", "0.01", *argv])

    def status(self):
        return train.status(self.root, REPO, self.clone, self.tools)

    def open_batch(self, note=None):
        return train.test(self.root, REPO, self.clone, self.tools, note)

    def repair(self):
        return train.repair(self.root, REPO, self.clone, self.tools)

    def promote(self, *argv):
        return train.promote(self.root, REPO, self.clone, self.tools, self.opts("promote", *argv))
