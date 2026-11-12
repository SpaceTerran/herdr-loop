"""Start, prompt and close the worker for one attempt.

    brief   render the attempt's brief from its template and the item's record
    start   count the attempt, open a pane, load its secrets, start the agent,
            and send it one line pointing at the brief
    prompt  send an open worker one line pointing at a file
    close   close the worker's pane, and stop what the worker left running
    end-run close the run's workspace, once its work is completed and shipped

`start` is one step on purpose. The attempt is counted against its bound
before anything is opened, and given back if the worker never came up, so a
phase can neither run uncounted nor be charged for a pane that failed to
open. One worker per item is open at a time: the mini has 8 GB.

A run has one herdr workspace, named for the work the run completes
(`hl-items init --work`), and every worker and served build of the run is a
tab in it: what runs at the same time sits side by side. Two runs worked at
once are two workspaces. The first `start` opens the workspace. Its first
tab is a shell in the run directory that the scripts never type into; it
keeps the workspace there between one worker and the next, and closing it
is how `end-run` closes the workspace. That happens only once every item of
the run is merged and verified. The manager's own workspace is never used
for workers.

A worker is an ordinary interactive session. A Claude worker gets its role's
tools and nothing else the host has installed: no slash commands, no user
settings, hooks or plugins, and only the MCP servers its role names. A
Cursor worker is launched with the operator's own flags and cannot be
restricted this way.

Only panes this script opened are ever closed, and only after checking the
pane still holds the shell that was started in it. Closing the pane does not
end what a worker's shell commands left in the background: those keep the
session of the shell that started them, which is not the pane's. Every shell
the worker starts writes itself down (`hl/reap.py`), and `close` stops what
is still running in those sessions once the pane is gone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import claim, confirm, contractfmt, herdr as herdr_mod, items, merge, program, reap, review, secrets as keeper, verdict
from .common import Refusal, absolute, emit, read_json, sha256_text, utc_now, write_json, write_text
from .herdr import Herdr, HerdrError

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")
TRUST_YES = "Yes, I trust this folder"
TRUST_NO = "No, exit"
# Claude draws its input box as a rule with a line under it that starts with this.
BOX_MARK = "\u276f"
RULE_MARK = "\u2500"
# How long Claude is given to draw that box after the trust dialog: 150 looks, 0.2 s apart.
BOX_LOOKS = 150
BOX_PAUSE = 0.2
# How many times a prompt that was not taken is helped along before it is left for a reader.
SUBMIT_TRIES = 3
# Herdr delivers a prompt longer than about 600 characters as pasted content,
# which a session is told may hold instructions its user did not write.
MAX_PROMPT = 500

# phase -> (role, letter in the agent's name, brief template)
PHASES = {
    "agree": ("evaluator", "a", "agree.md"),
    "contract-draft": ("builder", "d", "contract.md"),
    "build": ("builder", "b", "build.md"),
    "grade": ("evaluator", "g", "grade.md"),
    "correction": ("evaluator", "c", "correction.md"),
}
TOOLS = {"builder": "Bash,Read,Edit,Write,Glob,Grep", "evaluator": "Bash,Read,Glob,Grep,Write"}
# The tool that hands work to a sub-agent. Claude Code lists it as `Task` and calls it `Agent`; the hook matches both.
SUBAGENT_TOOL = "Agent"
SUBAGENT_MATCHER = "Agent|Task"
NO_PORT = "none (this item serves nothing)"
# The slot of a brief rendered once its build is served (review, confirm, blind grade), filled from `--note-file`.
NOTE = "MANAGER_NOTE"
NOTE_HELP = "a file holding a note for the worker, put into the brief's own slot for one (a review's, a confirm's or a grade's brief, which is rendered only once the build is served)"
AGENT_ARGV0 = {"claude": "claude", "cursor": "cursor-agent"}
FROM_FIELDS = {
    "BRANCH": "branch",
    "WORKTREE": "worktree",
    "CHECKOUT": "checkout",
    "PORT": "port",
    "PR_URL": "pr_url",
    "PR_NUMBER": "pr",
    "HEAD_SHA": "head_sha",
}


class Envs:
    """Whether a running process has a variable, without ever holding its value."""

    def has(self, pid: int, name: str) -> bool:
        out = subprocess.run([claim.PS, "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        return any(token.startswith(name + "=") for token in out.split())


def next_n(attempts: dict, phase: str) -> int:
    # A contract draft belongs to the agree round that will review it.
    return attempts["agree" if phase == "contract-draft" else phase] + 1


def program_slug(program_name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", program_name.lower())[:10] or "hl"


def agent_name(program_name: str, item_id: str, phase: str, n: int) -> str:
    name = f"{program_slug(program_name)}-{re.sub(r'[^a-z0-9]', '', item_id.lower())}-{PHASES[phase][1]}{n}"
    if not NAME_RE.match(name):
        raise Refusal(f"agent name {name!r} is not one herdr accepts; shorten the program name")
    return name


def _phase(phase: str) -> tuple[str, str, str]:
    if phase not in PHASES:
        raise Refusal(f"unknown phase {phase!r}; one of {', '.join(PHASES)}")
    return PHASES[phase]


# ---------------------------------------------------------------- brief


def brief_path(run_dir, item_id: str, phase: str, n: int) -> Path:
    return items.item_dir(run_dir, item_id) / f"{phase}-{n}.brief.md"


BLOCK_RE = re.compile(r"^### block: *(\S+) *$", re.MULTILINE)
# How `confirm.md` begins: what says, in the brief itself, that it is a delta confirm's.
BRIEF_SETS = {"v1": "briefs-v1", "v2": "briefs"}


def brief_file(cfg: dict, name: str) -> Path:
    """A brief template or block file. `briefs: v1` in program.json keeps the
    kit's first briefs, word for word; what those never had (a review, the
    blocks, a rework for red checks) comes from the current set either way."""
    references = Path(cfg["kit"]) / "references"
    chosen = references / BRIEF_SETS.get(cfg.get("briefs"), BRIEF_SETS["v2"]) / name
    return chosen if chosen.exists() else references / BRIEF_SETS["v2"] / name


def brief_blocks(cfg: dict) -> dict[str, str]:
    """The paragraphs of `briefs/blocks.md`, by name."""
    text = brief_file(cfg, "blocks.md").read_text()
    marks = list(BLOCK_RE.finditer(text))
    return {m.group(1): text[m.end() : (marks[i + 1].start() if i + 1 < len(marks) else len(text))].strip() for i, m in enumerate(marks)}


def brief_template(run_dir, phase: str, plan: dict | None = None) -> str:
    """Which template an attempt is briefed from. A run whose evaluators
    review the code briefs a grade from `review.md`, and a review that is
    a delta confirm (`plan`) from `confirm.md`."""
    name = _phase(phase)[2]
    if phase == "grade" and items.policy(items.load_run(run_dir))["review"] == "code":
        return "confirm.md" if (plan or {}).get("kind") == "delta" else "review.md"
    return name


class WrittenBeforeReview(Refusal):
    """Tracked files differ from the commit as the reviewer is about to
    start, after the caller found the checkout clean and served the build:
    the build's own Serve or Ready line wrote them, or the setup run again.
    `files` are those the program's spec does not name."""

    def __init__(self, item_id: str, files: list[str]):
        self.files = files
        shown = ", ".join(files[:8]) + (f" and {len(files) - 8} more" if len(files) > 8 else "")
        super().__init__(
            f"once the build of {item_id} is served, tracked files differ from the commit under review ({shown}): its own Serve or Ready line wrote them, or the "
            "setup run again. The reviewer would be given a build that is not the commit, so no review was started and none is spent"
        )


def how_run_of(serve: str, ready_kind: str, ready: str) -> str:
    """How a build is served and found ready, as one value. `hl-flow review`
    makes it from what `hl-serve` ran, and it is kept on the review's
    attempt: a rework that is served another way is no small fix."""
    return sha256_text(json.dumps({"serve": serve, "ready_kind": ready_kind, "ready": ready}, sort_keys=True))


def served_as(run_dir, item_id: str) -> str | None:
    """The same value from the Serve and Ready lines of the item's latest
    build report, or None where the report does not say. For a brief
    rendered before anything is served (`hl-spawn brief`): what a review is
    started with is what was served, and a report can be rewritten once
    its build is running."""
    report = items.item_dir(run_dir, item_id) / f"build-{items.load_attempts(run_dir, item_id)['build']}.md"
    try:
        how = contractfmt.how_to_run(report.read_text())
    except (OSError, contractfmt.ContractError):
        return None
    return how_run_of(how["serve"], how["ready_kind"], how["ready"])


def pinned_tools(root) -> merge.Tools:
    """The program's pinned git and gh, checked to be the files that were
    pinned: what every script of the kit that reads a repository uses.
    `hl-flow` hands its own down; this is for a caller that has none
    (`hl-spawn brief`, `hl-spawn start` by hand)."""
    bins = claim.check_pins(root, ("git", "gh"))["bins"]
    return merge.Tools(git=bins.get("git"), gh=bins.get("gh"))


def review_plan(cfg: dict, run_dir, item_id: str, full: bool = False, head: str | None = None, tools=None, how_run: str | None = None) -> dict:
    """Whether the review that would start next is a full review or a delta
    confirm, and why (`confirm.decide`). `head` is the commit to review
    when it is not the one on the item's record. `tools` are the caller's
    git and gh, where it has them (`hl-flow` has the pinned ones).

    The base is fetched first, as the merge gate fetches it before it asks
    the same questions: decided from a base that had since moved, a confirm
    would be started, refused at the gate, and the item's one confirm spent."""
    run = items.load_run(run_dir)
    fields = items.load_item(run_dir, item_id)["fields"]
    worktree, head = fields.get("worktree"), head or fields.get("head_sha")
    if items.policy(run)["confirm"] != "delta" or not head or not worktree or not Path(worktree).is_dir():
        # A run that gives every rework a full review, as the kit always did; or nothing to read a fix from, and
        # the brief, which needs both, says what is missing.
        return {"kind": "full", "why": None}
    base = items.base_of(run, cfg["base"])
    try:
        if tools is None:
            # The fetch reaches GitHub: on the loop's own token, like every script that does, or not at all.
            merge.require_loop_token(cfg["root"])
            tools = pinned_tools(cfg["root"])
        fetched = tools.git(["fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"], worktree, check=False)
        unfetched = fetched.stderr if fetched.returncode != 0 else None
    except Refusal as refusal:
        unfetched = str(refusal)
    if unfetched is not None:
        # In one line, like every reason: what git says runs over several.
        return {"kind": "full", "why": f"`{base}` could not be fetched, so where the pull request's base is now is not known ({' '.join(unfetched.split())[:200]})"}
    return confirm.decide(run_dir, item_id, head, tools, worktree, f"origin/{base}", program.repo_facts(cfg, run["repo"]), full, how_run)


def _bullets(lines, indent: str = "") -> str:
    return "\n".join(f"{indent}- {line}" for line in lines)


def _fill(text: str, known: dict) -> str:
    return PLACEHOLDER_RE.sub(lambda m: known.get(m.group(1)) if known.get(m.group(1)) is not None else m.group(0), text)


def _repo_facts_text(facts_of_repo: dict) -> str:
    notes = facts_of_repo.get("facts") or []
    return ("## About this repository\n\n" + _bullets(notes)) if notes else ""


def _builder_values(cfg: dict, run: dict, item: dict, values: dict, engine: str | None) -> dict:
    """What a build brief says that depends on the item's route, the
    program's settings and the repository's facts: whether the worktree
    arrived ready, how much to test before the push, whether to look, and
    whether sub-agents are open."""
    facts_of_repo = program.repo_facts(cfg, run["repo"])
    blocks = brief_blocks(cfg)
    claude = engine != "cursor"
    out = {"REPO_FACTS": _repo_facts_text(facts_of_repo)}
    ready = item["fields"].get("setup") == "ready" and facts_of_repo.get("setup")
    out["SETUP"] = _fill(blocks["setup-ready"], {**values, "SETUP_COMMAND": facts_of_repo.get("setup")}) if ready else ""
    if items.on_train(run):
        # Its pull request runs none of the full checks, whatever the program asks of a builder elsewhere.
        out["TESTING"] = _fill(blocks["testing-train"], {**values, "ITEM_CHECKS": _item_checks_text(run), "TRAIN_RED": _train_red(cfg, run, blocks)})
    else:
        out["TESTING"] = blocks["testing-touched" if cfg.get("prepush") == "touched" else "testing-full"]
    route = item.get("route")
    if (route or {}).get("look") or "look" in (item.get("floor") or []):
        out["LOOK"] = blocks["look-builder" if claude else "look-builder-none"]
    else:
        out["LOOK"] = blocks["no-look-builder" if route else "look-builder-unknown"]
    out["HELPERS"] = blocks["helpers" if claude and cfg.get("subagents") else "no-helpers"]
    protected = facts_of_repo.get("protected") or []
    listed = ", ".join(f"`{p}`" for p in protected)
    exempt = items.granted(run, item)
    if protected and exempt:
        # The builder is told what the merge lets through: otherwise it would leave alone the very file the item must change.
        plain = lambda text: " ".join(str(text or "").split()).replace("{{", "{ {")  # noqa: E731
        listed += "; this item alone may change " + ", ".join(f"`{path}` ({plain(record.get('reason'))})" for path, record in sorted(exempt.items()))
    out["PROTECTED"] = _fill(blocks["protected"], {"PROTECTED_LIST": listed}) if protected else ""
    return out


def _item_checks_text(run: dict) -> str:
    """The quick checks a run on a train still holds on an item's own pull request, as a sentence; nothing when it holds none."""
    asked = items.policy(run)["train"].get("item_checks") or []
    if not asked:
        return "No check is held on this pull request."
    names = ", ".join(f"`{name}`" for name in asked)
    return f"Held on this pull request before it merges, and nothing else: {names}."


def _train_red(cfg: dict, run: dict, blocks: dict) -> str:
    """What a worker on a train is told while the latest tested batch is red: where to read what fails, so nobody chases it."""
    from . import train  # here, not at the top: only a run on a train needs it

    red = train.red_file(cfg["root"], run["repo"])
    return _fill(blocks["train-red"], {"RED_FILE": str(red)}) if red.exists() else ""


def _dispute_text(d: Path, attempts: dict, grades: list[dict], criterion: str | None) -> str | None:
    """Where a correction reads the dispute from: the review that failed the
    criterion or could not check it, the builder's objection, or both."""
    build = attempts["build"]
    report = d / f"build-{build}.md"
    objected = build and report.exists() and contractfmt.disputed(report.read_text())
    graded = [e for e in grades if e.get("build") == build]
    parts = [f"criterion {criterion}." if criterion else None]
    if graded:
        n = graded[-1]["n"]
        parts.append(f"The verdict that failed it or could not check it: `{d}/verdict-{n}.json` (its note for that criterion, its evidence, and any finding about it).")
    if objected:
        parts.append(f"The builder's objection: the `## Disputed criterion` section of `{report}`. Read only that section of the file.")
        if not graded:
            parts.append("No review has run yet: the builder raised this from its own check of the criteria against the code.")
    if not graded and not objected:
        return None
    return " ".join(p for p in parts if p)


def _confirm_values(cfg: dict, run: dict, values: dict, plan: dict) -> dict:
    """What a delta confirm's brief carries: the fix list it answers, the
    two commits the fix lies between, what the fix touches, and the pages
    the failed review looked at."""
    blocks = brief_blocks(cfg)
    # A reviewer's words go into the brief as text: on one line, and never as a placeholder of the brief's own.
    plain = lambda text: " ".join(str(text or "").split()).replace("{{", "{ {")  # noqa: E731
    entries = []
    for k, fix in enumerate(plan["fix_list"], 1):
        about = f"Criterion {fix['criterion']}" if fix.get("criterion") is not None else "No criterion"
        where = f" (`{plain(fix['where'])}`)" if fix.get("where") else ""
        entries.append(f"  {k}. {about}: {plain(fix.get('what'))}{where}")
    out = {
        "FIRST_N": str(plan["confirms"]),
        "FROM_SHA": plan["from_sha"],
        "HEAD_SHA": plan["head_sha"],
        "FIX_LIST": "\n".join(entries),
        "DELTA_FACTS": "\n".join("  " + line for line in review.facts_text(plan["facts"]).splitlines()),
        "REPO_FACTS": _repo_facts_text(program.repo_facts(cfg, run["repo"])),
    }
    pages = sorted({(str(look.get("mode", "")).strip(), str(look.get("url", "")).strip()) for look in plan["looks"]})
    if pages:
        listed = "\n".join(f"  - `{plain(url)}`, mode `{plain(mode)}`" for mode, url in pages)
        out["LOOK"] = _fill(blocks["look-confirm"], {**values, **out, "LOOK_PAGES": listed}).strip()
    else:
        out["LOOK"] = _fill(blocks["no-look-confirm"], {**values, **out})
    if items.on_train(run):
        out["CHECKS"] = _fill(blocks["checks-confirm-train"], {**values, **out, "ITEM_CHECKS": _item_checks_text(run), "TRAIN_RED": _train_red(cfg, run, blocks)}).strip()
    else:
        out["CHECKS"] = blocks["checks-confirm"]
    return out


def _review_values(cfg: dict, run: dict, item: dict, d: Path, n: int, values: dict, used: set, plan: dict | None = None, tools=None) -> dict:
    """What a review brief carries beyond a grade's: the diff's facts, what
    the repository's checks run, and whether the item is one a person looks at."""
    if (plan or {}).get("kind") == "delta":
        return _confirm_values(cfg, run, values, plan)
    out: dict = {}
    facts_of_repo = program.repo_facts(cfg, run["repo"])
    blocks = brief_blocks(cfg)

    def filled(text: str, extra: dict | None = None) -> str:
        return _fill(text, {**values, **out, **(extra or {})})

    if "DIFF_FACTS" in used and values.get("WORKTREE") and values.get("HEAD_SHA"):
        try:
            # A confirm later rests on these facts (the merge base they record): read with the pinned git, like the rest.
            facts = review.diff_facts(tools or pinned_tools(cfg["root"]), values["WORKTREE"], f"origin/{items.base_of(run, cfg['base'])}", values["HEAD_SHA"], facts_of_repo)
        except (RuntimeError, OSError) as failure:
            raise Refusal(f"could not read the diff of {item['id']} in {values['WORKTREE']}: {failure}") from failure
        exempt = items.granted(run, item)
        if exempt and facts["protected"]:
            # What the manager recorded this item may change, so a reviewer does not take the change for a breach.
            facts["exempted"] = {path: exempt[path].get("reason") for path in facts["protected"] if path in exempt}
        write_json(d / f"grade-{n}.facts.json", facts)
        out["DIFF_FACTS"] = "\n".join("  " + line for line in review.facts_text(facts).splitlines())
    out["REPO_FACTS"] = _repo_facts_text(facts_of_repo)
    route = item.get("route")
    if (route or {}).get("look") or "look" in (item.get("floor") or []):
        out["LOOK"] = filled(blocks["look-review"]).strip()
    else:
        out["LOOK"] = filled(blocks["no-look-review" if route else "look-unknown-review"])
    covered, uncovered = facts_of_repo.get("ci_covers") or [], facts_of_repo.get("not_in_ci") or []
    not_in_ci = filled(blocks["not-in-ci"], {"NOT_IN_CI_LIST": _bullets(uncovered)}) if uncovered else ""
    if items.on_train(run):
        # The full checks run after the merge, on a batch: before it, this review is what there is.
        ran_later = _bullets(covered) if covered else "- (the program's spec does not say what they run)"
        said = {"CI_COVERS": ran_later, "NOT_IN_CI": not_in_ci, "ITEM_CHECKS": _item_checks_text(run), "TRAIN_RED": _train_red(cfg, run, blocks)}
        out["CHECKS"] = filled(blocks["checks-train"], said).strip()
    elif covered:
        out["CHECKS"] = filled(blocks["checks-known"], {"CI_COVERS": _bullets(covered), "NOT_IN_CI": not_in_ci}).strip()
    else:
        out["CHECKS"] = filled(blocks["checks-unknown"])
    return out


def _brief_values(
    cfg: dict, run_dir, item_id: str, phase: str, n: int, used: set | None = None, engine: str | None = None, sets: dict | None = None, plan: dict | None = None, tools=None
) -> dict:
    run = items.load_run(run_dir)
    item = items.load_item(run_dir, item_id)
    attempts = items.load_attempts(run_dir, item_id)
    d = items.item_dir(run_dir, item_id)
    fields = item["fields"]
    grades = [e for e in attempts["log"] if e["phase"] == "grade" and not e.get("voided")]
    values = {
        "ITEM_ID": item_id,
        "RUN_ID": run["run_id"],
        "N": str(n),
        "ITEM_DIR": str(d),
        "REPO": run["repo"],
        # Where the item's pull request goes: the program's base, or its repository's train.
        "BASE": items.base_of(run, cfg["base"]),
        "DEPLOY_BASE": cfg["base"],
        "CONTRACT_PATH": str(d / "contract.md"),
        "EVIDENCE_DIR": str(d / f"evidence-{n}"),
        "BUILD_N": str(attempts["build"]) if attempts["build"] else None,
        "GRADE_N": str(grades[-1]["n"]) if grades else None,
        **{name: fields.get(field) for name, field in FROM_FIELDS.items()},
    }
    values["CHECKOUT"] = values["CHECKOUT"] or values["WORKTREE"]
    # The slot a brief that is rendered only once its build is served keeps for the manager's note (`--note-file`): empty without one.
    values["MANAGER_NOTE"] = ""
    # An item that serves nothing has no port to record.
    values["PORT"] = values["PORT"] or NO_PORT

    report = d / f"build-{attempts['build']}.md"
    if report.exists():
        try:
            how = contractfmt.how_to_run(report.read_text())
            values["HOW_TO_RUN"] = f"**Serve.** `{how['serve']}`\n  **Ready.** {how['ready_kind']} `{how['ready']}`"
        except contractfmt.ContractError:
            pass
    served = d / f"serve-{attempts['build']}.json"
    values["REFERENCE"] = ""
    if served.exists():
        values["READY_SUMMARY"] = read_json(served).get("summary")
        reference = read_json(served).get("reference") or {}
        if reference.get("served") and not reference.get("stopped_at"):
            values["REFERENCE"] = _fill(brief_blocks(cfg)["reference"], {"REFERENCE_ROOT": reference["root"], "REFERENCE_URL": reference["url"]})

    values["REWORK_BLOCK"] = _rework_block(cfg, d, attempts, grades, values) if phase == "build" and n > 1 else ""
    values["REPO_FACTS"] = _repo_facts_text(program.repo_facts(cfg, run["repo"]))
    # Every worker of the current set is told the same about commands that never return: a stuck one spends the attempt.
    values["SHELL"] = brief_blocks(cfg)["shell"]
    if phase == "build":
        values.update(_builder_values(cfg, run, item, values, engine))
    if phase == "correction":
        values["DISPUTE"] = _dispute_text(d, attempts, grades, (sets or {}).get("CRITERION"))
    if phase == "grade" and items.policy(run)["review"] == "code":
        values.update(_review_values(cfg, run, item, d, n, values, used or set(), plan, tools))
    return values


def checks_file(item_dir, build: int) -> Path:
    """Where `hl-wait --checks` writes down required checks that finished red on a build's head."""
    return Path(item_dir) / f"checks-{build}.md"


def rework_note(item_dir, n: int) -> Path:
    """Where the manager says why build `n` is needed when no script has: a
    branch that conflicts with its base, a build that was sent back twice."""
    return Path(item_dir) / f"rework-{n}.md"


def _rework_block(cfg: dict, d: Path, attempts: dict, grades: list[dict], values: dict) -> str:
    """What a build after the first is told to fix from: the verdict that
    did not pass, the required checks that came out red, a note the manager
    wrote for this build, or any of them together. A build with none has
    nothing to fix, and is refused."""
    build = attempts["build"]
    style = items.policy(items.load_run(d.parent.parent))["review"]
    blocks = []
    graded = [e for e in grades if e.get("build") == build]
    if graded and verdict.check(d, graded[-1]["n"], style=style)["effective"] != "PASS":
        blocks.append((brief_file(cfg, "rework-block.md"), {"GRADE_N": str(graded[-1]["n"])}))
    red = checks_file(d, build)
    if red.exists():
        blocks.append((brief_file(cfg, "rework-checks-block.md"), {"CHECKS_FILE": str(red)}))
    note = rework_note(d, build + 1)
    if note.exists():
        blocks.append((brief_file(cfg, "rework-note-block.md"), {"REWORK_NOTE": str(note)}))
    if not blocks:
        raise Refusal(
            f"build {build} has nothing on record to fix: no verdict of it that did not pass, no {red.name} "
            f"(`hl-wait --checks` writes that file when required checks finish red), and no {note.name}. A further build "
            f"is a rework: when its reason is neither (the branch conflicts with its base, the build was sent back twice), "
            f"write what the builder must do in {note}, as facts, and start it again."
        )
    out = []
    for path, extra in blocks:
        filled = {**values, **extra}
        out.append(PLACEHOLDER_RE.sub(lambda m: filled.get(m.group(1)) or m.group(0), path.read_text()).strip())
    return "\n\n".join(out)


def brief_text(root, run_dir, item_id: str, phase: str, sets: dict[str, str] | None = None, engine: str | None = None, plan: dict | None = None, tools=None):
    """The brief for the attempt that would start next: where it goes, its
    text, and for a review the plan it was rendered from. Refused while
    any placeholder has no value, so a brief with a hole is never sent.
    `engine` is the worker the brief is for, where that changes what it
    says (a Cursor builder has no browser and no sub-agents).

    A review's brief is a full review's or a delta confirm's, as
    `review_plan` decides (`plan`, when the caller has already asked it).
    Nothing is written here."""
    _phase(phase)
    cfg = program.load(root)
    if phase == "grade" and plan is None:
        plan = review_plan(cfg, run_dir, item_id)
    template_name = brief_template(run_dir, phase, plan)
    n = next_n(items.load_attempts(run_dir, item_id), phase)
    template = brief_file(cfg, template_name).read_text()
    used = set(PLACEHOLDER_RE.findall(template))
    sets = sets or {}
    # A correction names its criterion with --set whichever set of briefs it is rendered from.
    unknown = sorted(set(sets) - used - ({"CRITERION"} if phase == "correction" else set()))
    if NOTE in unknown:
        raise Refusal(
            f"{template_name} has no slot for a manager's note: it is rendered before anything is served, so a note goes below it. "
            f"Render it (`hl-spawn brief --id {item_id} --phase {phase}`), write the note under what that writes, and start it without --note-file"
        )
    if unknown:
        raise Refusal(f"{template_name} has no placeholder named {', '.join(unknown)}")
    values = {**_brief_values(cfg, run_dir, item_id, phase, n, used, engine, sets, plan, tools), **sets}
    missing = sorted(name for name in used if values.get(name) is None)
    if missing:
        how = [
            f"{name}: record it with `hl-items set --field {FROM_FIELDS[name]}=<value>`"
            if name in FROM_FIELDS
            else "DISPUTE: nothing is in dispute yet (no review of this build, and its report disputes no criterion)"
            if name == "DISPUTE"
            else f"{name}: pass `--set {name}=<value>`"
            for name in missing
        ]
        raise Refusal([f"the {phase} brief needs a value it does not have. {line}" for line in how])
    text = PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)
    if PLACEHOLDER_RE.search(text):
        raise Refusal("a value given to the brief itself contains a placeholder")
    return brief_path(run_dir, item_id, phase, n), text, plan, n


def render_brief(root, run_dir, item_id: str, phase: str, sets: dict[str, str] | None = None, engine: str | None = None, plan: dict | None = None, tools=None) -> Path:
    """Write the brief for the attempt that would start next, for a manager
    to read, or to add notes of its own below (`hl-spawn brief`). `start`
    renders the brief again and sends a file only if it begins with that."""
    path, text, plan, n = brief_text(root, run_dir, item_id, phase, sets, engine, plan, tools)
    _write_brief(path, text, phase, plan, n)
    return path


def _write_brief(path: Path, text: str, phase: str, plan: dict | None, n: int) -> None:
    """The brief, whole or not there, and beside a confirm's brief the plan it was rendered from, for the record: nothing reads the plan back."""
    if phase == "grade" and plan["kind"] == "delta":
        write_json(confirm.plan_path(path.parent, n), plan)
    elif phase == "grade":
        confirm.plan_path(path.parent, n).unlink(missing_ok=True)
    write_text(path, text)


# ---------------------------------------------------------------- start


def _checkout_at_start(tools, worktree, root) -> dict | None:
    """The state of a review's checkout as the review starts (`review.checkout_state`), with the caller's git where it has one and the program's pinned one where not."""
    try:
        return review.checkout_state(tools or pinned_tools(root), worktree)
    except Refusal:
        return None


def worker_agents(cfg: dict) -> dict:
    """The sub-agent types the kit opens to a worker, by name."""
    return read_json(Path(cfg["kit"]) / "references" / "worker-agents.json")


def worker_settings(cfg: dict, item_dir: Path, phase: str, n: int) -> Path:
    """The settings file a Claude worker starts with. With sub-agents open it
    is the kit's file plus the guard hook, written beside the attempt: a
    hook's command is an absolute path, and the kit sits under the loop root.
    The guard keeps every sub-agent in the foreground, on the worker's own
    model, and refuses any type the kit does not define."""
    base = Path(cfg["kit"]) / "references" / "worker-settings.json"
    if not cfg.get("subagents"):
        return base
    settings = read_json(base)
    guard = Path(cfg["kit"]) / "scripts" / "hooks" / "agent-guard"
    # `|| exit 2`: a hook that cannot run at all is otherwise a warning, and the call it was to guard goes ahead.
    command = " ".join(shlex.quote(part) for part in (str(guard), *sorted(worker_agents(cfg)))) + " || exit 2"
    settings["hooks"] = {"PreToolUse": [{"matcher": SUBAGENT_MATCHER, "hooks": [{"type": "command", "command": command}]}]}
    path = item_dir / f"{phase}-{n}.settings.json"
    write_json(path, settings)
    return path


def effort_for(cfg: dict, role: str, rework: bool, asked: str | None = None) -> str:
    """The effort a Claude worker starts at. The manager may name one of the
    program's allowed efforts for any attempt; when it names none, a build
    after the first runs at the rework effort, and the rest at their role's."""
    if asked:
        if asked not in cfg["efforts"]:
            raise Refusal(f"effort {asked!r} is not one this program allows ({', '.join(cfg['efforts'])})")
        return asked
    if rework:
        return cfg["rework_effort"]
    by_role = cfg.get("build_effort") if role == "builder" else cfg.get("review_effort")
    return by_role or cfg["claude_effort"]


def _argv(
    cfg: dict, engine: str, role: str, ui: bool, session: str | None, resume: bool, effort: str | None, settings: Path | None = None
) -> list[str]:
    if engine == "cursor":
        # The operator's own launch line, plus the model.
        return ["--force", "--trust", "--approve-mcps", "--sandbox", "disabled", "--model", cfg["cursor_model"]]
    references = Path(cfg["kit"]) / "references"
    mcp = "mcp-playwright.json" if (role == "evaluator" or ui) else "mcp-none.json"
    subagents = bool(cfg.get("subagents"))
    argv = [
        "--dangerously-skip-permissions",
        "--model",
        cfg["claude_model"],
        "--effort",
        effort,
        "--tools",
        TOOLS[role] + (f",{SUBAGENT_TOOL}" if subagents else ""),
        "--disable-slash-commands",
        "--resume" if resume else "--session-id",
        session,
        "--setting-sources",
        "project,local",
        "--settings",
        str(settings or references / "worker-settings.json"),
        "--strict-mcp-config",
        "--mcp-config",
        str(references / mcp),
    ]
    if subagents:
        # The kit's own sub-agent types. Each names its tools, so none has a browser and none can start another.
        argv += ["--agents", agents_argument(cfg)]
    return argv


def agents_argument(cfg: dict) -> str:
    """The kit's sub-agent types as the one argument `--agents` takes in a
    terminal: the JSON itself. Claude Code (2.1.296) reads a file there only
    with `--print`, and a worker is not started that way: given the path, it
    printed an error and exited, and no worker with sub-agents ever came
    up. On one line, and with no quote mark of a shell's in it, so that
    nothing between here and the pane has a reason to cut it."""
    inline = json.dumps(worker_agents(cfg), separators=(",", ":"))
    return inline.replace("'", "\\u0027").replace("`", "\\u0060")


def _load_secrets(herdr, pane: str, cfg: dict, engine: str) -> None:
    """Fetch the worker's tokens inside its own pane. The sentinel is not in
    the typed line, so only the script's own success can match it."""
    nonce = secrets.token_hex(4)
    script = Path(cfg["kit"]) / "scripts" / "hl-env.sh"
    claude_ref = cfg["worker_claude_ref"] if engine == "claude" else "-"
    line = f". {shlex.quote(str(script))} {shlex.quote(claude_ref)} {shlex.quote(cfg['gh_token_ref'])} {nonce}"
    if keeper.in_use(cfg):
        # With the root as a fourth word the pane asks the program's keeper, which read each token once, in place
        # of the store (a 1Password service account counts every read against the account's limit for the day).
        # Without it the pane reads each token itself with `hl-read-secret`.
        line += f" {shlex.quote(cfg['root'])}"
    herdr.call("pane", "run", pane, line)
    sentinel = f"hl-env ok {nonce}-ready"
    try:
        # Either line ends the wait, so a failed read is reported at once and not after the timeout.
        waited = herdr.call("pane", "wait-output", pane, "--regex", f"hl-env (ok {nonce}-ready|FAILED {nonce}:)", "--timeout", "60000", timeout=80)
    except HerdrError as error:
        said = ""
        try:
            tail = herdr.text("pane", "read", pane, "--source", "recent-unwrapped", "--lines", "20")
            said = " | ".join(l.strip() for l in tail.splitlines() if l.strip().startswith("hl-env"))
        except HerdrError:
            pass
        raise Refusal(f"the worker's secrets did not load ({said or error}); no agent was started") from error
    matched = str(waited.get("matched_line") or "").strip()
    if sentinel not in matched:
        raise Refusal(f"the worker's secrets did not load ({matched or 'the wait ended without the ready line'}); no agent was started")


def set_claude_home(herdr, pane: str, cfg: dict) -> None:
    """Put the program's Claude seat into a pane's shell before a Claude
    session starts there: `CLAUDE_CONFIG_DIR`, the seat `program.claude_home`
    names. Nothing is typed for a program that names none, which runs on the
    host's default (staging refuses such a program). It fails closed: a directory that holds no sign-in is reported,
    and nothing is exported, so no session starts on an account by accident.
    The ready line carries a nonce in a form the typed line does not have."""
    home = program.claude_home(cfg)
    if not home:
        return
    nonce = secrets.token_hex(4)
    quoted = shlex.quote(home)
    line = (
        f"if [ -s {quoted}/.claude.json ]; then export CLAUDE_CONFIG_DIR={quoted}; printf 'hl-home ok %s-%s\\n' {nonce} ready; "
        f"else printf 'hl-home FAILED %s: no Claude sign-in in %s\\n' {nonce} {quoted}; fi"
    )
    herdr.call("pane", "run", pane, line)
    try:
        waited = herdr.call("pane", "wait-output", pane, "--regex", f"hl-home (ok {nonce}-ready|FAILED {nonce}:)", "--timeout", "30000", timeout=45)
    except HerdrError as error:
        raise Refusal(f"the pane did not take the program's Claude sign-in {home} ({error}); no agent was started") from error
    matched = str(waited.get("matched_line") or "").strip()
    if f"hl-home ok {nonce}-ready" not in matched:
        raise Refusal(f"the program's Claude sign-in did not load ({matched or 'the wait ended without the ready line'}); no agent was started")


def mark_shells(herdr, pane: str, cfg: dict, shells: Path) -> None:
    """Have every shell the worker starts write itself down in `shells`
    (`hl/reap.py`), the pane's own shell first, so that what the worker
    leaves running can be stopped when it is closed. Typed last before the
    agent starts: a keeper that `hl-env.sh` started from this pane is the
    program's, and does not carry the worker's mark. The ready line carries
    a nonce in a form the typed line does not have."""
    nonce = secrets.token_hex(4)
    herdr.call("pane", "run", pane, reap.pane_line(shells, cfg["kit"], nonce))
    try:
        waited = herdr.call("pane", "wait-output", pane, "--regex", f"hl-shells (ok {nonce}-ready|FAILED {nonce})", "--timeout", "30000", timeout=45)
    except HerdrError as error:
        raise Refusal(f"the pane did not take the line that has its shells write themselves down ({error}); no agent was started") from error
    matched = str(waited.get("matched_line") or "").strip()
    if f"hl-shells ok {nonce}-ready" not in matched:
        raise Refusal(f"the pane could not write its shell down in {shells} ({matched or 'the wait ended without the ready line'}); no agent was started")


def _reap(procs, shells) -> dict:
    """Stop what a worker left running (`hl/reap.py`), on the machine's processes unless the caller has others."""
    return reap.reap(procs or reap.Procs(), shells)


def _screen(herdr, name: str) -> str:
    return herdr.text("agent", "read", name, "--source", "visible", "--lines", "40")


def _box_drawn(screen: str) -> bool:
    lines = screen.splitlines()
    if any(TRUST_YES in l or TRUST_NO in l for l in lines):
        return False
    return any(l.startswith(BOX_MARK) and lines[i - 1].startswith(RULE_MARK) for i, l in enumerate(lines) if i)


def _accept_trust(herdr, name: str, error: HerdrError, sleep) -> dict:
    """Claude asks whether to trust a directory it has not seen, whatever
    flags it was given, and a new worktree is such a directory. Answer yes,
    by moving to that line: the default on this dialog is "No, exit".

    Then wait for Claude's input box. Herdr calls the agent idle and ready
    the moment the dialog is answered, while Claude is still starting, and
    what is typed before the box is drawn is lost in whole or in part."""
    info = herdr.agent(name)
    screen = _screen(herdr, name)
    lines = [l for l in screen.splitlines() if TRUST_YES in l or TRUST_NO in l]
    yes = next((i for i, l in enumerate(lines) if TRUST_YES in l), None)
    cursor = next((i for i, l in enumerate(lines) if "❯" in l), None)
    waiting = info and info.get("agent_status") == "blocked" and info.get("launch_pending")
    if not waiting or yes is None or cursor is None:
        raise Refusal(f"{name} stopped at start on something that is not the folder-trust dialog: {error.message}")
    keys = ["down"] * (yes - cursor) + ["up"] * (cursor - yes) + ["enter"]
    herdr.call("agent", "send-keys", name, *keys)
    herdr.call("agent", "wait", name, "--until", "idle", "--timeout", "30000", timeout=45)
    info = herdr.agent(name)
    if not info or not info.get("interactive_ready"):
        raise Refusal(f"{name} is not ready after the folder-trust dialog was answered")
    for _ in range(BOX_LOOKS):
        if _box_drawn(_screen(herdr, name)):
            return info
        sleep(BOX_PAUSE)
    raise Refusal(f"{name} did not draw its input box after the folder-trust dialog was answered")


def _last_lines(herdr, pane: str) -> str:
    """The last lines a pane shows, on one line, or nothing where Herdr cannot say."""
    try:
        text = herdr.text("pane", "read", pane, "--source", "recent-unwrapped", "--lines", "12")
    except HerdrError:
        return ""
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())[-700:]


def _start_agent(herdr, name: str, engine: str, pane: str, argv: list[str], sleep) -> dict:
    args = ("agent", "start", name, "--kind", engine, "--pane", pane, "--timeout", "120000", "--", *argv)
    for attempt in range(4):
        try:
            return herdr.call(*args, timeout=150)["agent"]
        except HerdrError as error:
            if error.code == "agent_pane_busy" and attempt < 3:
                sleep(3)
                continue
            if error.code == "agent_not_ready":
                return _accept_trust(herdr, name, error, sleep)
            raise
    raise Refusal(f"pane {pane} stayed busy; {name} was not started")


def _said(session: str | None, prompt: str) -> int:
    """How many times a Claude session's transcript holds this prompt as a message from its user."""
    path = herdr_mod.claude_transcript(session) if session else None
    if not path:
        return 0
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "user":
            continue
        content = (record.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
        count += prompt in str(content or "")
    return count


def _squash(text: str) -> str:
    """Text without its white space: a long line is wrapped on the screen, and the wrap is not part of it."""
    return "".join(text.split())


def _prompt(herdr, name: str, prompt: str) -> str | None:
    """Type the prompt and wait for herdr to see work begin. None when it did."""
    try:
        herdr.call("agent", "prompt", name, prompt, "--wait", "--until", "working", "--until", "blocked", "--timeout", "15000", timeout=30)
    except HerdrError as error:
        if error.code != "agent_prompt_stalled":
            raise
        return "herdr saw no work begin after the prompt was typed"
    return None


def submit(herdr, name: str, prompt: str, sleep, session: str | None = None) -> str | None:
    """Send an agent one prompt and see that it was taken. None when it
    was; otherwise what was seen instead, for whoever reads the pane next.

    `herdr agent prompt` answers ok for text that never reached the agent,
    so the call waits for herdr to see work begin. When herdr sees none,
    nothing is typed a second time unless the first is known not to have
    arrived: the agent is idle, the text is nowhere on its screen, and a
    Claude session's transcript has no such message. Text that sits typed
    in the input box gets the Enter it lost. `session` is a Claude
    session's id."""
    said_before = _said(session, prompt)
    seen = _prompt(herdr, name, prompt)
    for attempt in range(SUBMIT_TRIES + 1):
        if seen is None:
            return None
        status = (herdr.agent(name) or {}).get("agent_status")
        if status == "working" or _said(session, prompt) > said_before:
            return None
        if status not in ("idle", "done"):
            # A dialog, or a state nobody can type into: say so and leave it.
            return f"{seen}, and the agent is {status or 'gone'}"
        if attempt == SUBMIT_TRIES:
            break
        if attempt:
            sleep(2)
        if _squash(prompt) in _squash(_screen(herdr, name)):
            # Typed, and the Enter was lost. On an input box that holds nothing, Enter does nothing.
            herdr.call("agent", "send-keys", name, "enter")
            try:
                herdr.call("agent", "wait", name, "--until", "working", "--until", "blocked", "--timeout", "5000", timeout=20)
                seen = None
            except HerdrError as error:
                if error.code != "timeout":
                    raise
                seen = "the prompt is typed on the agent's screen and Enter did not start it"
        else:
            seen = _prompt(herdr, name, prompt)
    return seen


def workspace_record(run_dir) -> Path:
    return Path(run_dir) / items.WORKSPACE_FILE


def run_workspace(herdr, cfg: dict, run_dir) -> str:
    """The id of the run's workspace, opened when herdr has none for it.

    The workspace is named for the work the run completes. One that already
    carries that name is the run's too: the operator may have opened it for
    this work. Such a workspace is used and never closed from here."""
    run = items.load_run(run_dir)
    label = run.get("work") or f"{cfg['program']} {run['run_id']}"
    path = workspace_record(run_dir)
    record = read_json(path) if path.exists() else None
    listed = herdr.workspaces()
    if record and not record.get("closed_at"):
        mine = next((w for w in listed if w.get("workspace_id") == record["workspace"]), None)
        # Workspace ids are reused: the id is the run's while the name is, or while its first pane holds the same shell.
        if mine and (mine.get("label") == record["label"] or _holds(herdr, record.get("pane"), record.get("shell_pid"))):
            return record["workspace"]
    found = next((w["workspace_id"] for w in listed if w.get("label") == label), None)
    if found:
        write_json(path, {"label": label, "workspace": found, "pane": None, "tab": None, "shell_pid": None, "opened_here": False, "created_at": utc_now()})
        return found
    made = herdr.create_workspace(label, Path(run_dir).resolve())
    shell_pid = herdr.call("pane", "process-info", "--pane", made["pane"])["process_info"].get("shell_pid")
    write_json(path, {"label": label, **made, "shell_pid": shell_pid, "opened_here": True, "created_at": utc_now()})
    if made["tab"]:
        try:
            herdr.call("tab", "rename", made["tab"], run["run_id"])
        except HerdrError:
            pass
    return made["workspace"]


def _holds(herdr, pane: str | None, shell_pid) -> bool:
    if not pane or shell_pid is None:
        return False
    try:
        return herdr.call("pane", "process-info", "--pane", pane)["process_info"].get("shell_pid") == shell_pid
    except HerdrError:
        return False


def unshipped(run_dir) -> list[str]:
    """Why the run's work is not yet completed and shipped, item by item."""
    out = []
    for item_id in items.load_run(run_dir)["order"]:
        if items.load_item(run_dir, item_id)["status"] == "skipped":
            continue
        problem = items.shipped_problem(run_dir, item_id)
        if problem:
            out.append(problem)
    return out


def end_run(run_dir, herdr) -> dict:
    """Close the run's workspace, by closing its first pane. A workspace
    closes only when its work is completed and shipped: until then it stays
    in herdr, where the operator sees what is still open. Panes anyone else
    opened in it are not this script's; herdr keeps the workspace while one
    is left, and the answer says so."""
    path = workspace_record(run_dir)
    if not path.exists():
        raise Refusal(f"{path} does not exist: this run never opened a workspace")
    record = read_json(path)
    if record.get("closed_at"):
        return {"closed": True, "already": True, "workspace": record["workspace"]}
    in_use = []
    for d in sorted(p for p in (Path(run_dir) / "items").iterdir() if p.is_dir()):
        in_use += [f"{d.name}/{name}" for name in _open_workers(run_dir, d.name)]
        in_use += [f"{d.name}/{p.name}" for p in sorted(d.glob("serve-*.json")) if _being_served(read_json(p))]
    if in_use:
        raise Refusal(f"the run still has something open ({', '.join(in_use)}); close it with `hl-spawn close` or `hl-serve stop` first")
    open_work = unshipped(run_dir)
    if open_work:
        raise Refusal(
            "a workspace closes only when its work is completed and shipped; this run's is not: "
            + "; ".join(open_work)
            + ". The workspace stays open until it is."
        )
    note = "closed"
    if not record.get("pane"):
        note = "this run did not open the workspace, so it is left as it is"
    else:
        try:
            info = herdr.call("pane", "process-info", "--pane", record["pane"])["process_info"]
        except HerdrError as error:
            if not error.not_found:
                raise
            info, note = None, "the workspace's first pane was already gone"
        if info is not None and info.get("shell_pid") != record.get("shell_pid") and not _restored(herdr, record, info):
            note = "the pane id now belongs to something else; left alone"
        elif info is not None:
            herdr.call("pane", "close", record["pane"])
    left = any(w.get("workspace_id") == record["workspace"] and w.get("label") == record["label"] for w in herdr.workspaces())
    record.update(closed_at=utc_now(), close_note=note)
    write_json(path, record)
    return {"closed": True, "already": False, "workspace": record["workspace"], "label": record["label"], "note": note, "workspace_still_listed": left}


def _restored(herdr, record: dict, info: dict) -> bool:
    """The run's first pane with a new shell in it: herdr brought the
    workspace back after a restart or a reboot, and three ended runs'
    workspaces were left open that way (the help-desk catalog program,
    2026-11-09). It
    is the run's while the workspace keeps the run's id and name, the rule
    `run_workspace` follows, and the pane is in it. It is closed only while
    its shell runs nothing."""
    ours = any(w.get("workspace_id") == record["workspace"] and w.get("label") == record["label"] for w in herdr.workspaces())
    in_it = str(record.get("pane") or "").startswith(f"{record['workspace']}:")
    idle = all(p.get("pid") == info.get("shell_pid") for p in info.get("foreground_processes") or [])
    return ours and in_it and idle


def _being_served(record: dict) -> bool:
    return bool(record.get("served")) and not record.get("stopped_at")


def _open_workers(run_dir, item_id: str) -> list[str]:
    d = items.item_dir(run_dir, item_id)
    return sorted(p.name for p in d.glob("*.worker.json") if not read_json(p).get("closed_at"))


def start(
    root, run_dir, item_id: str, phase: str, engine: str, herdr, opts, envs=None, sleep=time.sleep, tools=None, began: dict | None = None, how_run: str | None = None, procs=None
) -> dict:
    """`began`, for a review: the state of its checkout when the caller took
    the review up, before it served the build (`hl-flow review`). A review
    begins on the commit: where no such state is given, the checkout as it
    is now is held to that. `how_run`, from the same caller: what it served
    (`how_run_of`). A review started with neither is never followed by a
    delta confirm.

    The brief is rendered here, every time. A file already at its place is
    sent only if it begins with exactly that text: what follows is the
    manager's notes, counted in the answer and kept as one value on the
    attempt. A brief a worker left there, or one rendered for a state that
    has changed since, does not begin so and is refused."""
    role = _phase(phase)[0]
    if role == "evaluator" and engine != "claude":
        raise Refusal("every evaluation is a Claude session; a Cursor worker can only build")
    cfg = program.load(root)
    root = Path(cfg["root"])
    envs = envs or Envs()
    item = items.load_item(run_dir, item_id)
    attempts = items.load_attempts(run_dir, item_id)
    d = items.item_dir(run_dir, item_id)
    n = next_n(attempts, phase)

    still_open = _open_workers(run_dir, item_id)
    if still_open:
        raise Refusal(f"{item_id} still has a worker open ({', '.join(still_open)}); close it with `hl-spawn close` first")
    if herdr_mod.worker_path(d, phase, n).exists():
        raise Refusal(
            f"{phase} {n} of {item_id} was started before and its worker record is still there. If that worker ended "
            f"without a result, void it with `hl-items void --id {item_id} --phase {phase} --n {n} --note <why>` and start again"
        )
    if (d / f"{phase}-{n}.done").exists():
        raise Refusal(f"{phase}-{n}.done already exists")
    # Said before a brief is rendered: past its bound, a phase has no brief to write.
    past = items.bound_reached(run_dir, item_id, phase)
    if past:
        raise Refusal(past)

    brief = brief_path(run_dir, item_id, phase, n)
    plan = baseline = None
    if phase == "grade":
        worktree = item["fields"].get("worktree")
        # The state of the checkout as the reviewer is about to start. It is judged here, before a brief is written or
        # an attempt counted, and the same state goes on the attempt: what the review did is read against it at the end.
        baseline = _checkout_at_start(tools, worktree, root)
        unnamed = review.unnamed_changes(baseline, program.repo_facts(cfg, items.load_run(run_dir)["repo"]).get("rewrites"))
        if began is not None and unnamed:
            raise WrittenBeforeReview(item_id, unnamed)
        # A caller that served nothing (`began` is its word that it looked before it served) has no moment before the
        # build was served: its checkout is held to the commit as it is now, but for the paths the spec names.
        held = baseline and {**baseline, "dirty": {path: baseline["dirty"][path] for path in unnamed}}
        served = d / f"serve-{attempts['build']}.json"
        serving = served.exists() and _being_served(read_json(served))
        if serving and read_json(served).get("checkout_changed"):
            raise Refusal(
                f"the build of {item_id} that is being served was started before its checkout was put back: stop it (`hl-serve stop --id {item_id}`), "
                f"serve it again (`hl-serve start --id {item_id}`), and run this again"
            )
        changed = review.begins_changed(held, worktree, item_id, opts.head_sha or item["fields"].get("head_sha"), item["fields"].get("branch"), held and (tools or pinned_tools(root)))
        if changed and serving:
            # Put back under a build that is still up, the checkout would be the commit and the build would not: noted on the build's own record.
            write_json(served, {**read_json(served), "checkout_changed": True})
            raise Refusal(
                f"a build of {item_id} is being served from this checkout as it is: stop it first (`hl-serve stop --id {item_id}`). {changed[0].upper()}{changed[1:]}. "
                f"Then serve the build again (`hl-serve start --id {item_id}`)"
            )
        if changed:
            raise Refusal(changed)
        plan = review_plan(cfg, run_dir, item_id, opts.full, opts.head_sha, tools, how_run)
    brief, text, plan, _ = brief_text(root, run_dir, item_id, phase, opts.sets, engine, plan, tools)
    was = brief.read_text() if brief.exists() else text
    if not was.startswith(text):
        raise Refusal(
            f"{brief} does not begin with the brief this script renders for {phase} {n} of {item_id} now: something else wrote or changed it, or what it "
            f"was rendered from has changed since (a review's brief says what the served build answered, so one rendered before the build was served never "
            f"does). Remove the file and run this again. To send notes of your own with a brief, render it (`hl-spawn brief --id {item_id} --phase {phase}`, "
            "with the same `--set` values as here), and put them below what that writes"
        )
    added = [line for line in was[len(text) :].splitlines() if line.strip()]
    notes = {"lines": len(added), "sha256": sha256_text(was[len(text) :])} if added else None
    note_file = getattr(opts, "note_file", None)
    if NOTE in (opts.sets or {}):
        # The manager's note is part of the brief the script rendered; the record says which file it came from, as one value.
        notes = {**(notes or {"lines": 0}), "manager_note": {"file": str(note_file) if note_file else None, "sha256": sha256_text(opts.sets[NOTE])}}
    if PLACEHOLDER_RE.search(was):
        raise Refusal(f"{brief.name} still contains a placeholder")
    prompt = f"Read {brief} and do exactly what it says."
    if len(prompt) > MAX_PROMPT:
        raise Refusal(f"the pointer to the brief is {len(prompt)} characters; over {MAX_PROMPT} it arrives as pasted content")

    fields = item["fields"]
    if opts.cwd:
        cwd = Path(opts.cwd).resolve()
    elif role == "evaluator":
        # An evaluator works beside the item, never in the repository: what it
        # and its browser write must not show up in the builder's worktree.
        cwd = (d / (f"evidence-{n}" if phase == "grade" else f"{phase}-{n}.work")).resolve()
        cwd.mkdir(exist_ok=True)
    else:
        cwd = Path(fields["worktree"]).resolve() if fields.get("worktree") else None
    if cwd is None or not cwd.is_dir():
        raise Refusal(f"no working directory for the worker: record `--field worktree=<path>` or pass --cwd (got {cwd})")
    if root not in cwd.parents and cwd != root:
        raise Refusal(f"{cwd} is outside the loop root {root}; a worker only ever runs inside it")

    name = agent_name(cfg["program"], item_id, phase, n)
    previous = [e for e in attempts["log"] if e["phase"] == "build" and not e.get("voided")]
    resume = (
        phase == "build" and engine == "claude" and bool(previous) and previous[-1].get("engine") == "claude" and not opts.fresh
    )
    if engine == "claude":
        session = previous[-1]["session"] if resume else str(uuid.uuid4())
    else:
        session = f"cursor:{name}"
    binaries = (AGENT_ARGV0[engine], "herdr") if engine == "cursor" else ("claude", "herdr")
    claim.check_pins(root, binaries)
    asked = getattr(opts, "effort", None)
    if engine == "cursor" and asked:
        raise Refusal("--effort is for a Claude worker; a Cursor worker runs the program's one Cursor model")
    effort = effort_for(cfg, role, phase == "build" and n > 1, asked) if engine == "claude" else None
    why = (getattr(opts, "why", None) or "").strip() or None
    # A builder whose item changes something a person looks at needs to look at it too.
    ui = opts.ui or (role == "builder" and bool((item.get("route") or {}).get("look")))

    drafter = None
    if phase == "agree":
        draft = herdr_mod.worker_path(d, "contract-draft", n)
        drafter = read_json(draft).get("items_session") if draft.exists() else None
    if phase != "contract-draft":
        head_sha = (opts.head_sha or fields.get("head_sha")) if phase == "grade" else None
        entry = items.start_phase(
            run_dir,
            item_id,
            phase,
            session=session,
            engine=engine,
            head_sha=head_sha,
            drafter_session=drafter,
            effort=effort,
            why=why,
            confirm=plan if (plan or {}).get("kind") == "delta" else None,
            baseline=baseline,
            how_run=how_run,
            notes=notes,
        )
        if entry["n"] != n:
            raise Refusal(f"attempt numbering disagrees: expected {phase} {n}, counted {entry['n']}")

    # Written only now, when nothing above can still refuse: a brief left by a start that was refused would not
    # begin with what the next start renders, and that start would refuse its own file.
    _write_brief(brief, was, phase, plan, n)
    pane = None
    shells = reap.shells_path(d, phase, n)
    try:
        workspace = opts.workspace or run_workspace(herdr, cfg, run_dir)
        made = herdr.call("tab", "create", "--workspace", workspace, "--cwd", str(cwd), "--label", f"{item_id} {phase}-{n}", "--no-focus")
        pane, tab = made["root_pane"]["pane_id"], made["tab"]["tab_id"]
        _load_secrets(herdr, pane, cfg, engine)
        if engine == "claude":
            set_claude_home(herdr, pane, cfg)
        settings = worker_settings(cfg, d, phase, n) if engine == "claude" else None
        argv = _argv(cfg, engine, role, ui, session if engine == "claude" else None, resume, effort, settings)
        mark_shells(herdr, pane, cfg, shells)
        _start_agent(herdr, name, engine, pane, argv, sleep)
        info = herdr.call("pane", "process-info", "--pane", pane)["process_info"]
        pid = next((p.get("pid") for p in info.get("foreground_processes", []) if p.get("argv0") == AGENT_ARGV0[engine]), None)
        own_token = engine == "claude" and cfg["worker_claude_ref"] != program.SIGNED_IN
        needed = ("CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN") if own_token else ("GH_TOKEN",)
        if engine == "claude" and program.claude_home(cfg):
            # The proof it runs on the program's sign-in and not the host's default.
            needed += ("CLAUDE_CONFIG_DIR",)
        # The proof its shells write themselves down: without them, what it leaves running cannot be found.
        needed += (reap.VAR, "ZDOTDIR")
        absent = [v for v in needed if pid is None or not envs.has(pid, v)]
        if absent:
            # Without its own token a Claude worker would run on the manager's account. Only a program whose
            # worker_claude_ref is `-` runs its Claude workers there, and that is the operator's choice.
            raise Refusal(f"{name} (pid {pid}) is running without {', '.join(absent)}")
    except (Refusal, HerdrError) as failure:
        # What the pane showed, read before the pane goes: a worker that printed why it would not start has said it there and nowhere else.
        shown = _last_lines(herdr, pane) if pane else ""
        stopped = None
        if pane:
            try:
                herdr.call("pane", "close", pane)
            except HerdrError:
                pass
            # An agent that came up and was refused may have started something already (an MCP server, a browser).
            stopped = reap.said(_reap(procs, shells))
        if phase != "contract-draft":
            items.void_attempt(run_dir, item_id, phase, n, f"spawn: the worker did not come up: {failure}")
        reasons = failure.reasons if isinstance(failure, Refusal) else [str(failure)]
        said = (f" The pane's last lines: {shown}" if shown else "") + (f" Its pane is closed, and the script {stopped}." if stopped else "")
        raise Refusal([f"{phase} {n} of {item_id} was not started and the attempt was given back. {r}{said}" for r in reasons])

    record = {
        "item": item_id,
        "phase": phase,
        "n": n,
        "agent": name,
        "kind": engine,
        "pane": pane,
        "tab": tab,
        "workspace": workspace,
        "shell_pid": info.get("shell_pid"),
        "pid": pid,
        "cwd": str(cwd),
        "items_session": session,
        "resumed": resume,
        "brief": str(brief),
        "shells": str(shells),
        "started_at": utc_now(),
        "model": cfg["claude_model"] if engine == "claude" else cfg["cursor_model"],
        "effort": effort,
        "browser": engine == "claude" and (role == "evaluator" or ui),
        "subagents": engine == "claude" and bool(cfg.get("subagents")),
    }
    if engine == "claude":
        record["session"] = session
    herdr_mod.write_worker(d, phase, n, record)

    # How many lines lay below the script's own brief and went with it: a manager that added none sees that something did.
    out = {"started": True, "watch": f"{item_id}:{phase}-{n}", **record, "prompt_sent": True, "brief_notes": notes["lines"] if notes else 0}
    if notes and notes.get("manager_note"):
        out["manager_note"] = notes["manager_note"]["file"] or True
    if (plan or {}).get("why"):
        # Which review this is and why, in a line: the script's choice, said to whoever started it.
        out["confirm"] = f"{plan['kind']}: {plan['why']}"
    try:
        unsent = submit(herdr, name, prompt, sleep, session if engine == "claude" else None)
    except HerdrError as error:
        unsent = str(error)
    if unsent:
        # Never sent again blind: the brief may already be running.
        out["prompt_sent"] = False
        out["warning"] = f"the prompt was not confirmed ({unsent}); read the pane before sending anything"
    return out


# ---------------------------------------------------------------- prompt / close


def send(run_dir, item_id: str, phase: str, n: int, file: str, herdr, sleep=time.sleep) -> dict:
    d = items.item_dir(run_dir, item_id)
    record = herdr_mod.read_worker(d, phase, n)
    if record.get("closed_at"):
        raise Refusal(f"{record['agent']} was closed at {record['closed_at']}")
    path = Path(file).resolve()
    if not path.is_file() or d.resolve() not in path.parents:
        raise Refusal(f"{file} is not a file in the item directory {d}")
    prompt = f"Read {path} and do exactly what it says."
    if len(prompt) > MAX_PROMPT:
        raise Refusal(f"the pointer is {len(prompt)} characters; over {MAX_PROMPT} it arrives as pasted content")
    unsent = submit(herdr, record["agent"], prompt, sleep, record.get("session"))
    if unsent:
        raise Refusal(f"{record['agent']} did not take the prompt ({unsent}); read its pane before sending anything")
    return {"prompted": True, "agent": record["agent"], "file": str(path), "watch": f"{item_id}:{phase}-{n}"}


def close(run_dir, item_id: str, phase: str, n: int, herdr, sleep=time.sleep, procs=None) -> dict:
    """Close the worker's pane, then stop what it left running: the
    processes still in a session one of its shells led (`hl/reap.py`). What
    was stopped, and anything KILL did not end, goes on the worker record."""
    d = items.item_dir(run_dir, item_id)
    record = herdr_mod.read_worker(d, phase, n)
    if record.get("closed_at"):
        return {"closed": True, "already": True, "agent": record["agent"]}
    note = "closed"
    try:
        info = herdr.call("pane", "process-info", "--pane", record["pane"])["process_info"]
    except HerdrError as error:
        if not error.not_found:
            raise
        info, note = None, "the pane was already gone"
    if info is not None and info.get("shell_pid") != record.get("shell_pid"):
        # Pane ids are reused. This one no longer holds the shell that was started for the worker.
        note = "the pane id now belongs to something else; left alone"
    elif info is not None:
        herdr.call("pane", "close", record["pane"])
        for _ in range(15):
            if herdr.agent(record["agent"]) is None:
                break
            sleep(1)
        else:
            raise Refusal(f"{record['agent']} is still there after pane {record['pane']} was closed")
    out = {"closed": True, "already": False, "agent": record["agent"]}
    if record.get("shells"):
        # Whatever became of the pane: its id may be another's now, and the worker's processes are found by their sessions.
        reaped = _reap(procs, record["shells"])
        record["reap"] = {"at": utc_now(), **reaped}
        stopped = reap.said(reaped)
        note = f"{note}; {stopped}" if stopped else note
        out.update(reaped=reaped["reaped"], left=reaped["left"])
        if reaped["left"]:
            out["warning"] = f"still running after KILL: {', '.join(str(p['pid']) for p in reaped['left'])}; read what they are (`ps -o pid,ppid,stat,command -p <pid>`) before the next attempt"
    else:
        note = f"{note}; its shells were not written down (started by an older kit), so nothing it left running was looked for"
    record.update(closed_at=utc_now(), close_note=note)
    herdr_mod.write_worker(d, phase, n, record)
    return {**out, "note": note}


# ---------------------------------------------------------------- cli


def _sets(pairs: list[str], note_file: str | None = None) -> dict[str, str]:
    out = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_]+", name):
            raise Refusal(f"--set takes NAME=value with NAME in capitals, got {pair!r}")
        out[name] = value
    if note_file:
        out[NOTE] = note_text(note_file)
    return out


def note_text(path) -> str:
    """A manager's note as a review brief carries it, in the slot the brief
    keeps for one. A review's brief says what the served build answered, so
    it is rendered only once the build is up, in the same command that
    starts the reviewer: a note written below an earlier rendering never
    matched it, and `hl-flow review` refused (an item's third review, in a
    help-desk feature program). The
    note is read from a file and put into the rendering itself, so every
    rendering of the brief carries it the same way."""
    path = Path(path)
    try:
        text = path.read_text().strip()
    except OSError as failure:
        raise Refusal(f"--note-file {path} cannot be read: {failure}") from failure
    if not text:
        raise Refusal(f"--note-file {path} is empty: leave the option out for a brief without a note")
    # Written into the brief as text: a `{{` in it is not a placeholder of the brief's own, as with any value from outside.
    return f"## A note from the manager\n\n{text.replace('{{', '{ {')}\n"


def note_slot_problem(cfg: dict, run_dir, item_id: str, phase: str) -> str | None:
    """Why a manager's note cannot go into the brief of the next `phase`
    attempt, or None. Asked before anything is served: every template the
    attempt could be briefed from must keep the slot (a review may be given a
    full review's brief or a delta confirm's), and the first set's grade brief
    keeps none."""
    policy = items.policy(items.load_run(run_dir))
    names = ["review.md", "confirm.md"] if phase == "grade" and policy["review"] == "code" else [_phase(phase)[2]]
    lacking = [name for name in names if f"{{{{{NOTE}}}}}" not in brief_file(cfg, name).read_text()]
    if not lacking:
        return None
    return (
        f"{', '.join(lacking)} of this program's briefs has no slot for a manager's note: it is rendered before anything is served, or it is of "
        f"the first set of briefs. Render it (`hl-spawn brief --id {item_id} --phase {phase}`), write the note under what that writes, and start it without --note-file"
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-spawn", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=absolute, required=True, help="the program's loop root")
    p.add_argument("--run-dir", required=True, type=absolute)
    p.add_argument("--herdr", help="absolute path of the herdr binary (default: the pinned one)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(s, with_n=False):
        s.add_argument("--id", required=True)
        s.add_argument("--phase", required=True, choices=list(PHASES))
        if with_n:
            s.add_argument("--n", required=True, type=int)

    s = sub.add_parser("brief", help="render the brief for the attempt that would start next")
    common(s)
    s.add_argument("--set", dest="sets", action="append", default=[], metavar="NAME=value")
    s.add_argument("--note-file", type=absolute, help=NOTE_HELP)
    s.add_argument("--engine", choices=["claude", "cursor"], help="the worker the brief is for (default: claude)")
    s.add_argument("--full", action="store_true", help="for a review: a full review's brief, where the script would give a delta confirm's")

    s = sub.add_parser("start", help="count the attempt, start the worker and send it its brief")
    common(s)
    s.add_argument("--engine", required=True, choices=["claude", "cursor"])
    s.add_argument("--set", dest="sets", action="append", default=[], metavar="NAME=value")
    s.add_argument("--note-file", type=absolute, help=NOTE_HELP)
    s.add_argument("--ui", action="store_true", help="give a Claude builder a browser (evaluators always have one; a builder whose item is routed with a look gets one by itself)")
    s.add_argument("--effort", help="the effort this Claude worker starts at, one of the program's `efforts` (default: by role, from program.json)")
    s.add_argument("--why", help="for a build: why this builder and effort, in a line; recorded with the attempt")
    s.add_argument("--fresh", action="store_true", help="a rework in a new session instead of the first builder's")
    s.add_argument("--cwd", help="working directory (default: a builder's worktree; a directory beside the item for an evaluator)")
    s.add_argument("--workspace", help="herdr workspace id (default: the run's own, named for its work)")
    s.add_argument("--head-sha", help="the commit to grade (default: the item's head_sha field)")
    s.add_argument("--full", action="store_true", help="for a review: a full review, where the script would give a delta confirm")

    s = sub.add_parser("prompt", help="send an open worker one line pointing at a file in the item directory")
    common(s, with_n=True)
    s.add_argument("--file", required=True)

    s = sub.add_parser("close", help="close the worker's pane")
    common(s, with_n=True)

    sub.add_parser("end-run", help="close the run's workspace, once every item is merged and verified and nothing is open")
    return p


def main(argv: list[str] | None = None, herdr=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "brief":
            plan = review_plan(program.load(args.root), args.run_dir, args.id, args.full, how_run=served_as(args.run_dir, args.id)) if args.phase == "grade" else None
            path = render_brief(args.root, args.run_dir, args.id, args.phase, _sets(args.sets, args.note_file), args.engine, plan=plan)
            # For a review in a run whose reworks may get a delta confirm: which of the two this brief is, and why.
            emit({"brief": str(path), **({"confirm": f"{plan['kind']}: {plan['why']}"} if (plan or {}).get("why") else {})})
            return 0
        if herdr is None:
            if os.environ.get("HERDR_ENV") != "1":
                raise Refusal("not inside a Herdr pane (HERDR_ENV is not 1); workers are only driven from inside Herdr")
            herdr = Herdr(args.herdr or claim.check_pins(args.root, ("herdr",))["bins"]["herdr"])
        if args.cmd == "start":
            args.sets = _sets(args.sets, args.note_file)
            emit(start(args.root, args.run_dir, args.id, args.phase, args.engine, herdr, args))
        elif args.cmd == "prompt":
            emit(send(args.run_dir, args.id, args.phase, args.n, args.file, herdr))
        elif args.cmd == "close":
            emit(close(args.run_dir, args.id, args.phase, args.n, herdr))
        elif args.cmd == "end-run":
            emit(end_run(args.run_dir, herdr))
    except (Refusal, HerdrError) as failure:
        reasons = failure.reasons if isinstance(failure, Refusal) else [str(failure)]
        emit({"refused": reasons})
        print(f"hl-spawn: refused: {'; '.join(reasons)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
