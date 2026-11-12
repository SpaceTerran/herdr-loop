"""Stage a program's loop root from one spec file. Safe to run again.

    hl-stage --spec <root>/ledger/<task folder>/program.spec.json

The spec sits beside the program's backlog files in your control
repository. `scripts/stage.sh` clones that repository to `<root>/ledger` on
the program's own branch and then runs this from the clone, so the kit that
is installed is the kit at that commit. This script then makes everything
else a manager needs:

    <root>/.claude/skills/herdr-loop   the kit, copied whole, and KIT-COMMIT
    <root>/CLAUDE.md                   the control repository's guardrails, when it has a CLAUDE.md
    <root>/program.json                settings the scripts read
    <root>/repos/<stream>/<repo>       one clone per stream, with the commit hook
    <root>/MANAGER-PROMPT.md           the manager prompt, filled
    <ledger>/<task folder>/LEDGER.md   the ledger, if the program has none yet
    <root>/runs/<run id>               each stream's first run, named for its work

Run again, it installs a newer kit and rewrites the prompt, and leaves the
ledger, the runs and every worktree as they are. It never touches Herdr.
A kit change belongs between items: a worker that is open is named in the
answer.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import backlog, clocks, items, outside, program, review
from .common import Refusal, emit, read_json, utc_now, write_json

EXCLUDES = (".worktrees/", ".playwright-mcp/", ".scratch/")
# Prose only the person setting the program up can write.
PROMPT_REQUIRED = ("FILES_AND_GATES", "PRODUCTION_BOUNDARY", "CAPACITY", "ACCOUNT_SHARING")
# Phrases that hand a gate to someone other than the manager (references/authoring.md, "Gates are the manager's").
# Staging remarks on them and refuses nothing: the operator's decision of 2026-10-16 is guidance, not a script rule.
GATE_HANDOFFS = (
    (re.compile(r"authoring session", re.I), "names the authoring session as a party: a gate is the manager's, done the loop's way"),
    (re.compile(r"never do (?:them|it)\b", re.I), "tells the manager never to do a gate"),
    (re.compile(r"out of bounds[^.]*\b(?:root|credential)", re.I), "puts a credential out of the manager's reach: it may use any credential the vault holds"),
    (re.compile(r"\b(?:no|never a) (?:loop worker or )?manager uploads\b", re.I), "forbids the manager an upload: uploads are the manager's; submitting for review is the operator's"),
)


def gate_remarks(prompt: dict) -> list[str]:
    """One line per phrase in the spec's prompt prose that keeps a gate from the manager, by key."""
    out = []
    for key in sorted(prompt):
        for pattern, why in GATE_HANDOFFS:
            if pattern.search(str(prompt[key])):
                out.append(f"{key} {why}")
    return out


# A backlog file the spec's prose names: `{{TASK_DIR}}/HD-SEARCH-ITEMS.md`.
NAMED_FILE_RE = re.compile(r"\{\{TASK_DIR\}\}/([\w./-]+?\.md)\b")


def named_backlogs(spec: dict, task_dir: Path) -> list[str]:
    """The backlog files a spec names, in order: each stream's first run, then those its prose names that exist and
    hold items (a guide it names, such as AUTHORING.md, holds none and is left out)."""
    names = [stream["run"]["file"] for stream in spec["streams"]]
    for key in sorted(spec["prompt"]):
        names += NAMED_FILE_RE.findall(str(spec["prompt"][key]))
    out = []
    for name in dict.fromkeys(names):
        path = task_dir / name
        try:
            if path.is_file() and backlog.parse_backlog(path.read_text()):
                out.append(name)
        except backlog.BacklogError:
            pass  # a stream's own file is refused by name when its run is made; any other is not this check's
    return out


def clock_remarks(spec: dict, task_dir: Path) -> list[str]:
    """One line per wait, clock or run edge in the spec's prompt prose (its gates) and in the backlog files it names
    (each file's header, and each item's text but its Verify line). Remarks for whoever wrote them, never a refusal:
    the operator's rule of 2026-11-10 is that nothing waits to see whether it fixes itself (hl/clocks.py)."""
    hits = []
    for key in sorted(spec["prompt"]):
        hits += [(key, kind, says) for kind, says in clocks.found(str(spec["prompt"][key]))]
    for name in named_backlogs(spec, task_dir):
        hits += clocks.backlog_hits((task_dir / name).read_text(), name)
    return clocks.remarks(hits)


# What a spec may say about a repository, once, for every brief and script that needs it:
#   facts         sentences every worker in the repository is told (a tool version CI uses, a known condition
#                 of the test host that is not a finding)
#   setup         one command line that makes a fresh checkout ready (dependencies installed)
#   setup_inputs  the files that command's result depends on (lockfiles, requirement files)
#   ci_covers     what the required checks run, one line per check
#   not_in_ci     suites no required check runs
#   seen          glob patterns of the paths that hold what a person sees
#   protected     glob patterns of the paths no item may change (generated or pinned files)
#   rewrites      glob patterns of the tracked paths the repository's own setup or served build rewrites by itself;
#                 a review starts over such a file, and over no other tracked file that differs from the commit
#   train         the repository's items merge into an integration branch and its required checks run on batches
#                 of them (`hl-train`): `{"batch": 5, "limit": 15}`, either number optional
#   item_checks   with a train: the checks an item's own pull request must still show green (quick ones; may be none)
#   reference     one directory of the repository holding the pages a look is compared with (`brand`): served from
#                 the item's checkout beside the build of an item a person looks at, since the browser refuses file://
#   exemptable    glob patterns of the protected paths the manager may exempt for one item (`hl-items exempt`); left
#                 out, none may be: fixed into each run when it is created
#   deploys       the workflow files that deploy the repository on a push to the base (`apply-main.yml`), `[]` when a
#                 merge deploys nothing: an outside pull request's deploy check (`hl-verify --outside`) reads their runs
#                 on its merge commit. Left out, no outside pull request merges there: its deploy could not be checked
REPO_FACT_LISTS = ("facts", "setup_inputs", "ci_covers", "not_in_ci", "seen", "protected", "rewrites", "item_checks", "exemptable", "deploys")
REPO_FACT_KEYS = ("setup", "train", "reference") + REPO_FACT_LISTS
# Paths no repository has: a `rewrites` pattern that covers all three covers every path.
ANY_PATH = ("zq9k", "zq9k/zq9k.zq9k", ".zq9k/zq9k")
PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def git(cwd, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise Refusal(f"git {' '.join(args[:3])} failed in {cwd}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def load_spec(path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise Refusal(f"{path} does not exist")
    spec = read_json(path)
    problems = [f"`{key}` is missing" for key in ("program", "root", "ledger_branch", "streams", "prompt") if not spec.get(key)]
    if problems:
        raise Refusal([f"{path.name}: {p}" for p in problems])
    root = Path(spec["root"])
    if not root.is_absolute():
        problems.append("`root` must be an absolute path")
    names = set()
    for i, stream in enumerate(spec["streams"]):
        where = f"streams[{i}]"
        for key in ("name", "writable", "ports", "run"):
            if not stream.get(key):
                problems.append(f"{where}: `{key}` is missing")
        if not (stream.get("repo") or spec.get("repo")):
            problems.append(f"{where}: no `repo`, and the spec has no default")
        for key in ("id", "work", "file"):
            if not (stream.get("run") or {}).get(key):
                problems.append(f"{where}.run: `{key}` is missing")
        if stream.get("name") in names:
            problems.append(f"{where}: the name {stream['name']!r} is used twice")
        names.add(stream.get("name"))
    problems += [f"prompt: `{key}` is missing" for key in PROMPT_REQUIRED if not spec["prompt"].get(key)]
    for i, stream in enumerate(spec["streams"]):
        base = stream.get("port_base")
        if base is not None and (not isinstance(base, int) or isinstance(base, bool) or base < 1024):
            problems.append(f"streams[{i}]: `port_base` must be a port number of 1024 or more, got {base!r}")
        if "setup" in stream and stream["setup"] is not False:
            problems.append(f"streams[{i}]: `setup` may only be `false` (this stream's items do not need the repository's setup); the command itself is the repository's")
    settings = spec.get("settings") or {}
    # Said here, on the host the program runs on: the directory must hold a sign-in before a manager starts on it.
    home_problem = program.claude_home_problem(settings.get("claude_config_dir"))
    if home_problem:
        problems.append(f"settings: {home_problem}")
    for adopt_problem in (
        outside.setting_problem(settings.get(outside.SETTING)),
        outside.vetters_problem(settings.get(outside.VETTERS)),
        outside.shared_login_problem(settings.get(outside.SHARED_LOGIN)),
    ):
        if adopt_problem:
            problems.append(f"settings: {adopt_problem}")
    if "confirm" in settings or "confirm_bound" in settings:
        try:
            # Said here, before anything is staged, and not when the first run is made.
            items.check_policy({key: settings.get(key, default) for key, default in (("review", "code"), ("confirm", "full"), ("confirm_bound", None))})
        except Refusal as refusal:
            problems += [f"settings: {reason}" for reason in refusal.reasons]
    repos = spec.get("repos") or {}
    named = {s.get("repo") or spec.get("repo") for s in spec["streams"]}
    if not isinstance(repos, dict):
        problems.append("`repos` must map a repository to its facts")
        repos = {}
    for repo, facts in repos.items():
        if repo not in named:
            problems.append(f"repos: {repo!r} is not the repository of any stream ({', '.join(sorted(str(n) for n in named))})")
        if not isinstance(facts, dict):
            problems.append(f"repos[{repo}]: must be an object")
            continue
        for key in facts:
            if key not in REPO_FACT_KEYS:
                problems.append(f"repos[{repo}]: `{key}` is not a fact the kit reads ({', '.join(REPO_FACT_KEYS)})")
        if "setup" in facts and (not isinstance(facts["setup"], str) or not facts["setup"].strip()):
            problems.append(f"repos[{repo}]: `setup` must be one command line")
        for key in REPO_FACT_LISTS:
            value = facts.get(key, [])
            if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
                problems.append(f"repos[{repo}]: `{key}` must be a list of strings")
        if facts.get("setup_inputs") and not facts.get("setup"):
            problems.append(f"repos[{repo}]: `setup_inputs` without `setup`")
        if facts.get("exemptable") and not facts.get("protected"):
            problems.append(f"repos[{repo}]: `exemptable` without `protected`: only a protected path can be exempted")
        if "reference" in facts:
            ref = facts["reference"]
            parts = ref.strip("/").split("/") if isinstance(ref, str) else []
            if not isinstance(ref, str) or not ref.strip("/ ") or ref.startswith("/") or any(p in ("", ".", "..") for p in parts) or set("*?[]{}") & set(ref):
                problems.append(f"repos[{repo}]: `reference` must be one directory of the repository, as a relative path (`brand`), got {ref!r}")
        for pattern in facts.get("rewrites") if isinstance(facts.get("rewrites"), list) else []:
            # A pattern that covers any path at all would let a review start over whatever its build wrote.
            if isinstance(pattern, str) and review.matching(list(ANY_PATH), [pattern]) == list(ANY_PATH):
                problems.append(f"repos[{repo}]: `rewrites` pattern {pattern!r} matches every path; name the files the repository rewrites by itself")
        if "train" in facts:
            train = facts["train"]
            whole = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1  # noqa: E731
            if not isinstance(train, dict) or any(k not in ("batch", "limit") for k in train) or any(not whole(v) for v in train.values()):
                problems.append(f"repos[{repo}]: `train` must be an object with whole numbers `batch` and `limit`, either left out (`{{}}` takes both defaults)")
            elif train.get("limit", 15) < train.get("batch", 5):
                problems.append(f"repos[{repo}]: a train's `limit` is the most it may hold untested, and cannot be less than its `batch`")
            if (spec.get("settings") or {}).get("review", "code") != "code":
                problems.append(f"repos[{repo}]: a train needs reviews that read the code (`settings.review` is `code`): on a train the full checks run after the merge")
        elif facts.get("item_checks"):
            problems.append(f"repos[{repo}]: `item_checks` without `train`: an item that merges into the base is held to the repository's required checks")
    if problems:
        raise Refusal([f"{path.name}: {p}" for p in problems])
    return spec


def fill(text: str, values: dict, what: str) -> str:
    """Fill `{{NAME}}` placeholders, twice, so a value may use one itself."""
    for _ in range(2):
        text = PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)
    left = sorted(set(PLACEHOLDER_RE.findall(text)))
    if left:
        raise Refusal(f"{what}: no value for {', '.join('{{' + name + '}}' for name in left)}")
    return text


def _install_kit(source: Path, dest: Path) -> None:
    """Replace the installed kit with the source, whole: a file the newer kit dropped must not linger."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fresh, old = dest.with_name(dest.name + ".new"), dest.with_name(dest.name + ".old")
    for leftover in (fresh, old):
        shutil.rmtree(leftover, ignore_errors=True)
    shutil.copytree(source, fresh, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if dest.exists():
        os.rename(dest, old)
    os.rename(fresh, dest)
    shutil.rmtree(old, ignore_errors=True)


def _clone(spec: dict, stream: dict, root: Path, kit: Path, base: str) -> dict:
    repo = stream.get("repo") or spec["repo"]
    clone = root / "repos" / stream["name"] / repo.split("/")[-1]
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        remote = f"{spec.get('remote_base', 'https://github.com').rstrip('/')}/{repo}.git"
        # `base_only`: the clone holds the base branch and the loop's own item branches and nothing else of the
        # target, for a program whose workers must not see what other branches of it hold.
        only = ["--single-branch", "--branch", base] if spec.get("base_only") else []
        proc = subprocess.run(["git", "clone", "--quiet", *only, remote, str(clone)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise Refusal(f"could not clone {repo}: {proc.stderr.strip()[:300]}")
        if only:
            git(clone, "config", "--add", "remote.origin.fetch", "+refs/heads/hl/*:refs/remotes/origin/hl/*")
    git(clone, "fetch", "--quiet", "--prune", "origin")
    # The primary checkout holds no work: it sits detached at the base and is only ever moved to it.
    if not git(clone, "status", "--porcelain"):
        git(clone, "checkout", "--quiet", "--detach", f"origin/{base}")
    exclude = clone / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    have = exclude.read_text().splitlines() if exclude.exists() else []
    missing = [line for line in EXCLUDES if line not in have]
    if missing:
        exclude.write_text("\n".join(have + missing) + "\n")
    hook = clone / ".git" / "hooks" / "commit-msg"
    hook.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(kit / "scripts" / "hooks" / "commit-msg", hook)
    hook.chmod(0o755)
    return {"stream": stream["name"], "repo": repo, "clone": str(clone), "head": git(clone, "rev-parse", "--short", "HEAD")}


def _claude_account_text(settings: dict) -> str:
    """The manager prompt's sentences on which Claude seats the program's sessions run on."""
    pool = program.claude_seats(settings)
    percent = settings.get("seat_full_percent", program.DEFAULTS["seat_full_percent"])
    start = (
        f"You and every Claude session the kit starts for this program are on the sign-in in `{pool[0]}` (`program.json` `claude_config_dir`) "
        "until the watchdog moves the program: `hl-watchdog` sets `CLAUDE_CONFIG_DIR` to the program's seat before it starts a manager, "
        "and `hl-spawn` before it starts a Claude worker, which refuses to come up without it."
    )
    if len(pool) == 1:
        return start + (
            f" When its five-hour or weekly window reaches {percent:g} %, the watchdog pauses the program until it resets (`seat` in "
            "`references/troubles.md`). The operator chooses the seats; do not change them."
        )
    others = ", ".join(f"`{home}`" for home in pool[1:])
    return start + (
        f" Its other seats are {others}. The watchdog decides which seat the program runs on: it keeps "
        f"the program on a seat with room, below {percent:g} % of both its five-hour and weekly windows, places it on another seat "
        "with room when its own fills, and pauses it while no seat has room, until the first one resets "
        "(`seat` in `references/troubles.md`; `scripts/hl-seat --root {{ROOT}} status` shows the readings). The operator chooses the "
        "seats; do not change them."
    )


def _streams_text(spec: dict, root: Path) -> str:
    lines = []
    for stream in spec["streams"]:
        repo = stream.get("repo") or spec["repo"]
        clone = root / "repos" / stream["name"] / repo.split("/")[-1]
        lines.append(
            f"  - `{stream['name']}` ({stream['run']['work']}): repository `{repo}`, clone `{clone}`. "
            f"Writable paths: {stream['writable']}. {stream['ports']}"
        )
    return "\n".join(lines)


def _open_workers(root: Path) -> list[str]:
    out = []
    for record in sorted(root.glob("runs/*/items/*/*.worker.json")):
        if not read_json(record).get("closed_at"):
            out.append(str(record.relative_to(root)))
    return out


def kit_tests(scripts: Path, pythons=None) -> list[dict]:
    """The installed kit's own tests, under the Python a pane uses and the one launchd falls back to."""
    out = []
    env = {**os.environ, "GH_TOKEN": ""}
    for python in dict.fromkeys(pythons or (sys.executable, "/usr/bin/python3")):
        if not Path(python).exists():
            continue
        proc = subprocess.run([python, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=scripts, capture_output=True, text=True, env=env)
        last = (proc.stderr.strip().splitlines() or ["no output"])[-1]
        out.append({"python": python, "passed": proc.returncode == 0, "result": last})
    return out


def stage(spec_path, kit_source=None, run_tests: bool = True) -> dict:
    spec_path = Path(spec_path).resolve()
    spec = load_spec(spec_path)
    root = Path(spec["root"])
    ledger = Path(git(spec_path.parent, "rev-parse", "--show-toplevel")).resolve()
    if ledger != (root / "ledger").resolve():
        raise Refusal(f"the spec is in {ledger}, not in {root / 'ledger'}: run scripts/stage.sh, which clones the ledger there first")
    branch = git(ledger, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != spec["ledger_branch"]:
        raise Refusal(f"the ledger clone is on {branch!r}; the spec names {spec['ledger_branch']!r}")
    task = spec_path.parent.relative_to(ledger)
    kit_source = Path(kit_source).resolve() if kit_source else ledger / ".claude" / "skills" / "herdr-loop"
    if not (kit_source / "scripts" / "hl-items").exists():
        raise Refusal(f"{kit_source} is not the kit: the ledger branch must carry .claude/skills/herdr-loop")
    # The seats, with the template's settings beneath the spec's as program.json will have them; said before anything is installed.
    seat_problems = program.seats_problems({**read_json(kit_source / "references" / "program.template.json"), **(spec.get("settings") or {})})
    if seat_problems:
        raise Refusal([f"settings: {problem}" for problem in seat_problems])
    base = spec.get("base", "main")
    for stream in spec["streams"]:
        if not (spec_path.parent / stream["run"]["file"]).is_file():
            raise Refusal(f"{task / stream['run']['file']} does not exist in the ledger clone")
    in_flight = _open_workers(root)

    # The kit, pinned at the commit the ledger clone holds.
    kit = root / ".claude" / "skills" / "herdr-loop"
    kit_commit = git(ledger, "log", "-1", "--format=%h", "--", ".claude/skills/herdr-loop") or git(ledger, "rev-parse", "--short", "HEAD")
    _install_kit(kit_source, kit)
    (root / "KIT-COMMIT").write_text(kit_commit + "\n")
    if (ledger / "CLAUDE.md").exists():
        shutil.copyfile(ledger / "CLAUDE.md", root / "CLAUDE.md")

    settings = {**read_json(kit / "references" / "program.template.json"), "program": spec["program"], "base": base}
    previous = read_json(root / "program.json") if (root / "program.json").exists() else None
    if previous is not None:
        # A program staged again keeps whether it adopts outside pull requests, whatever the template now says:
        # a kit merge never changes a live program. Its spec may still say so outright.
        settings[outside.SETTING] = previous.get(outside.SETTING) or "off"
        # The stewards' logins and a shared-login acceptance come from the spec alone: removing one there revokes it.
    settings.update(spec.get("settings") or {})
    ledger_file = ledger / task / "LEDGER.md"
    # What the scripts need from the spec, so the manager types none of it: each stream's clone and port base,
    # each repository's facts, and where the ledger is.
    settings["streams"] = {
        stream["name"]: {
            "repo": stream.get("repo") or spec["repo"],
            "clone": str(root / "repos" / stream["name"] / (stream.get("repo") or spec["repo"]).split("/")[-1]),
            **({"port_base": stream["port_base"]} if stream.get("port_base") is not None else {}),
            **({"setup": False} if stream.get("setup") is False else {}),
        }
        for stream in spec["streams"]
    }
    settings["repos"] = spec.get("repos") or {}
    settings["ledger"] = str(ledger_file)
    write_json(root / "program.json", settings)

    clones = [_clone(spec, stream, root, kit, base) for stream in spec["streams"]]

    topic = (settings.get("ntfy_url") or "").rstrip("/").rsplit("/", 1)[-1]
    values = {
        "PROGRAM": spec["program"],
        "ROOT": str(root),
        "KIT_COMMIT": kit_commit,
        "LEDGER": str(ledger_file),
        "LEDGER_BRANCH": spec["ledger_branch"],
        "TASK_DIR": str(ledger / task),
        "REPO": spec.get("repo") or ", ".join(sorted({s["repo"] for s in spec["streams"]})),
        "BASE": base,
        "CURSOR_MODEL": settings["cursor_model"],
        "WORKER_CLAUDE_REF": settings["worker_claude_ref"],
        "CLAUDE_ACCOUNT": _claude_account_text(settings),
        "GH_TOKEN_REF": settings["gh_token_ref"],
        "NTFY_TOPIC": topic or "none is set: report in your replies only",
        "STREAMS": _streams_text(spec, root),
        "ROUTING_NOTES": "none beyond the defaults below.",
        **spec["prompt"],
    }
    template = (kit / "references" / "manager-prompt.template.md").read_text()
    # Everything below the first line that is exactly "---" is the prompt.
    body = template.split("\n---\n", 1)[1].lstrip("\n")
    (root / settings.get("manager_prompt", "MANAGER-PROMPT.md")).write_text(fill(body, values, "the manager prompt"))

    ledger_created = False
    if not ledger_file.exists():
        text = fill((kit / "references" / "ledger.template.md").read_text(), {"PROGRAM": spec["program"], "UTC": utc_now()}, "the ledger")
        ledger_file.write_text(text)
        git(ledger, "add", str(task / "LEDGER.md"))
        git(ledger, "commit", "--quiet", "-m", f"{spec['program']}: start the ledger")
        ledger_created = True
    git(ledger, "push", "--quiet", "-u", "origin", spec["ledger_branch"])

    runs = []
    remarks: list[str] = []
    for stream in spec["streams"]:
        run = stream["run"]
        run_dir = root / "runs" / run["id"]
        repo = stream.get("repo") or spec["repo"]
        try:
            for parsed in backlog.parse_backlog((ledger / task / run["file"]).read_text()):
                remarks += backlog.lint(parsed)
        except backlog.BacklogError:
            pass  # init_run below refuses it by name
        policy = program.run_policy(settings, repo)
        items.init_run(run_dir, run["id"], repo, [str(ledger / task / run["file"])], work=run["work"], policy=policy, stream=stream["name"])
        nxt = items.next_item(run_dir)
        entry = {"stream": stream["name"], "run": run["id"], "work": run["work"], "run_dir": str(run_dir), "state": nxt["state"], "next": nxt["next"]}
        # A Verify line corrected in the file since the run began: the run keeps its own until `verify-line` loads it.
        drift = items.verify_drift(run_dir)
        if drift:
            entry["verify_lines_to_load"] = [f"hl-items --run-dir {run_dir} verify-line --id {i} --note <what was wrong>" for i in drift]
        runs.append(entry)

    tests = kit_tests(kit / "scripts") if run_tests else []
    failed = [t for t in tests if not t["passed"]]
    if failed:
        raise Refusal([f"the installed kit's tests fail under {t['python']}: {t['result']}" for t in failed])

    return {
        "staged": str(root),
        "program": spec["program"],
        "kit_commit": kit_commit,
        "ledger": str(ledger_file),
        "ledger_created": ledger_created,
        "clones": clones,
        "runs": runs,
        "tests": tests,
        # For whoever wrote the items: remarks on their Accept lists, never a refusal (references/authoring.md).
        "accept_remarks": remarks,
        # For whoever wrote the spec: prose that hands a gate to a person, never a refusal (references/authoring.md).
        "gate_remarks": gate_remarks(spec["prompt"]),
        # For whoever wrote the spec and the items: a wait on a clock, a reading or another run's end (references/authoring.md).
        "clock_remarks": clock_remarks(spec, ledger / task),
        "open_workers": in_flight,
        "warning": "the kit changed under workers that are still open; a kit change belongs between items" if in_flight else None,
        "start": f"{kit}/scripts/hl-watchdog --root {root} --herdr <path of herdr> start --note \"first start\"",
    }


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-stage", description=__doc__.split("\n\n")[0])
    p.add_argument("--spec", required=True, help="program.spec.json, inside the program's ledger clone")
    p.add_argument("--kit-source", help="the kit to install (default: the one in the ledger clone)")
    p.add_argument("--no-tests", action="store_true", help="do not run the installed kit's tests")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        emit(stage(args.spec, args.kit_source, not args.no_tests))
    except Refusal as refusal:
        emit({"refused": refusal.reasons})
        print(f"hl-stage: refused: {'; '.join(refusal.reasons)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
