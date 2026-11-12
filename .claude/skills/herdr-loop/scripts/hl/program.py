"""A program's settings: `program.json` in the loop root.

Written once at setup from `references/program.template.json`. The scripts
that start things (workers, the manager) read it, so the manager never has
to type a model string or a secret reference.
"""

from __future__ import annotations

import os
from pathlib import Path

from .common import Refusal, read_json

REQUIRED = ("program", "base", "worker_claude_ref", "gh_token_ref", "cursor_model")
# `worker_claude_ref` set to this names no token: by the operator's choice, the program's Claude workers run on
# the host's signed-in Claude account, as its manager does, and no Claude token is read for them.
SIGNED_IN = "-"
DEFAULTS = {
    "claude_model": "claude-sonnet-5-5",
    "claude_effort": "high",
    "rework_effort": "high",
    # What a build or a review runs at when the manager names no effort; None means claude_effort.
    "build_effort": None,
    "review_effort": None,
    # The efforts a manager may ask for. `low` is left out: at low Sonnet 5.5 can report a change done
    # without running a check, and stops to ask more often. `max` is left out for its cost.
    "efforts": ["medium", "high", "xhigh"],
    # The rules new runs start under (hl-items init --root). A program.json without them keeps the kit's first behaviour.
    "premise": "always",
    "review": "blind",
    "bounds": {},
    # What reviews a rework: `full`, a full fresh review of the new head, or `delta`, a fresh reviewer that
    # confirms a small fix against the failed review's fix list (`hl/confirm.py`; it needs `review: code`).
    # `confirm_bound` is how large such a fix may be; what it leaves out comes from `items.CONFIRM_BOUND`.
    "confirm": "full",
    "confirm_bound": {},
    # What staging learned from the spec: each stream's clone, and each repository's facts
    # (what its checks run, how a checkout is made ready, which paths a person sees or no item may change).
    "streams": {},
    "repos": {},
    # Whether a review may start while the pull request's required checks are still running (the merge holds
    # them green either way), or only once they have passed.
    "review_with_ci": False,
    # The context size, in tokens, at which `hl-flow save` tells a manager to hand over. None: it is not told.
    "handover_tokens": None,
    # The KB of settled history (Decisions but the newest 40, settled gates, follow-ups of finished items) at which
    # `hl-flow save` answers `archive.due`, and the manager moves it out with `hl-flow archive` (hl/archive.py).
    # None: the save says nothing about it, as before.
    "ledger_archive_kb": None,
    # Minutes an item may wait on its repository's required checks (`checks`) and from its merge to a passing
    # Verify (`verify`), as medians of the last merged items; `hl-flow save` says which repositories are over
    # (hl/pace.py). 15 minutes of checks is where the operator asked for the next lever (a help-desk program, 2026-11-02).
    "ci_budget_minutes": {"checks": 15, "verify": 20},
    # Whether `hl-merge --outside` may merge a pull request from outside the program that a steward on this host
    # labeled `rollout-ready` (`adopt`), or not (`off`, the kit's first behaviour). hl/outside.py says why.
    "outside_prs": "off",
    # The stewards' GitHub logins: only a `rollout-ready` label and a `rollout-ready-head:` line one of them wrote count.
    # None named, nothing is adopted whatever `outside_prs` says.
    "outside_pr_vetters": [],
    # `accept`: the operator accepts that a steward's login is also the login the loop's workers act as, so a worker
    # could mark its own pull request ready. `refuse` (the default): no outside pull request merges then.
    "outside_pr_shared_login": "refuse",
    # Arguments added to the manager's launch line. `{{KIT}}` is the installed kit's directory.
    "manager_args": [],
    # The program's LEDGER.md, written by staging.
    "ledger": None,
    # Which briefs workers are given (references/briefs-v1 or references/briefs), and whether a builder runs the
    # repository's suites in full before it pushes (`full`) or tests what it touched and leaves the full run to
    # the required checks (`touched`).
    "briefs": "v1",
    "prepush": "full",
    # Whether Claude workers get the kit's own sub-agents (references/worker-agents.json), behind the guard hook.
    "subagents": False,
    # Where the scripts get the program's secrets. `direct` is the kit's first behaviour: a manager exports
    # GH_TOKEN itself, every worker pane reads its tokens with `scripts/hl-read-secret` each time a worker starts,
    # and the watchdog reads with the program's reader. `keeper`: `hl-secrets` reads each secret once, holds it
    # in memory, and reads it again when it is `secrets_ttl_s` seconds old.
    "secrets": "direct",
    "secrets_ttl_s": 12 * 60 * 60,
    # The command that reads one secret, as a list of words; the reference is added as its last, the value is
    # what it prints, and its first line on stderr is the reason when it fails. The keeper and the watchdog use
    # it. None: the kit's `scripts/hl-read-secret` (file:, op://, keychain: and env: references). A worker pane
    # with `secrets: direct` is given no program and always reads with `hl-read-secret`.
    "secrets_reader": None,
    "manager_model": "claude-opus-5-5",
    "manager_effort": "high",
    # The Claude sign-in the program's manager and Claude workers start on: a configuration directory, set as
    # CLAUDE_CONFIG_DIR before each starts. None: the first of `claude_seats`, or with none the host's default,
    # `~/.claude`. It must be a `~/.claude-<name>` beside it, where the scripts look for transcripts
    # (`herdr.claude_homes`). It and `claude_seats` may be written with a leading `~/`: every script reads them
    # through `claude_seats` below, which expands it to the home directory of the user the loop runs as.
    "claude_config_dir": None,
    # Every sign-in the program may run on, each its own usage windows (`seat.py`). When the one in use is full
    # the watchdog moves the program to another that is open, and pauses it while every one is full.
    "claude_seats": None,
    # A seat is full when its five-hour or its weekly window is at this percentage or more.
    "seat_full_percent": 99,
    "manager_prompt": "MANAGER-PROMPT.md",
    "ntfy_url": None,
    "ntfy_user": None,
    "ntfy_password_ref": None,
}


def kit(root) -> Path:
    return Path(root) / ".claude" / "skills" / "herdr-loop"


def load(root) -> dict:
    path = Path(root) / "program.json"
    if not path.exists():
        raise Refusal(f"{path} does not exist: copy references/program.template.json there and fill it in")
    data = read_json(path)
    missing = [key for key in REQUIRED if not data.get(key)]
    if missing:
        raise Refusal(f"{path} is missing: {', '.join(missing)}")
    unfilled = sorted(key for key, value in data.items() if isinstance(value, str) and "{{" in value)
    if unfilled:
        raise Refusal(f"{path} still has unfilled placeholders in: {', '.join(unfilled)}")
    return {**DEFAULTS, **data, "root": str(Path(root).resolve()), "kit": str(kit(root).resolve())}


def claude_seats(cfg: dict) -> list[str]:
    """The Claude sign-ins the program may run on, the one it starts on
    first: `claude_config_dir`, then `claude_seats` in their order."""
    listed = [cfg.get("claude_config_dir")] + list(cfg.get("claude_seats") or [])
    out: list[str] = []
    for value in listed:
        if isinstance(value, str) and value.strip():
            path = os.path.expanduser(value)
            if path not in out:
                out.append(path)
    return out


def claude_home(cfg: dict) -> str | None:
    """The configuration directory the program's Claude sessions start with
    now: the seat the watchdog last moved the program to (`seat.json` in the
    root), else its first seat, or None for the host's default."""
    pool = claude_seats(cfg)
    if not pool:
        return None
    if cfg.get("root"):
        try:
            moved = (read_json(Path(cfg["root"]) / "seat.json") or {}).get("home")
        except (OSError, ValueError, AttributeError):
            moved = None
        if moved in pool:
            return moved
    return pool[0]


def seats_problems(settings: dict) -> list[str]:
    """Why a program's seats cannot be used. Said at staging, on the host the
    program runs on, against its settings with the template's beneath them.
    A loop never runs on the host's default sign-in: that account is the
    operator's own (the operator's rule, 2026-11-04)."""
    raw = settings.get("claude_seats")
    if raw is not None and (not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw)):
        return ["`claude_seats` must be a list of Claude configuration directories"]
    pool = claude_seats(settings)
    if not pool:
        return [
            "the program names no Claude seat (`claude_config_dir`, `claude_seats`): loops run on the loop's own seats, "
            "and the host's default sign-in is the operator's own"
        ]
    problems = [problem for problem in (claude_home_problem(path, "claude_seats") for path in pool) if problem]
    if len(pool) > 1 and settings.get("worker_claude_ref") != SIGNED_IN:
        problems.append(
            "`worker_claude_ref` must be `-` for a program with more than one seat: a token keeps the Claude workers "
            "on its own account when the program moves to another seat"
        )
    percent = settings.get("seat_full_percent", DEFAULTS["seat_full_percent"])
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 50 <= percent <= 100:
        problems.append(f"`seat_full_percent` must be a number from 50 to 100, got {percent!r}")
    return problems


def claude_home_problem(value, setting: str = "claude_config_dir") -> str | None:
    """Why a `claude_config_dir` cannot be used, or None. Said at staging,
    on the host the program runs on, before anything is installed."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return f"`{setting}` must be the path of a Claude configuration directory, or left out"
    path = Path(os.path.expanduser(value))
    home = Path(os.path.expanduser("~"))
    # Not `~/.claude` itself: set as CLAUDE_CONFIG_DIR it makes Claude read `~/.claude/.claude.json`, not the
    # default sign-in in `~/.claude.json`. That sign-in is the operator's, and no program runs on it.
    if not path.is_absolute() or path.parent != home or not path.name.startswith(".claude-") or path.name == ".claude-":
        return f"`{setting}` must be a {home}/.claude-<name> directory beside {home}/.claude, where the scripts look for transcripts; got {value!r}"
    try:
        signed_in = bool((read_json(path / ".claude.json") or {}).get("oauthAccount"))
    except (OSError, ValueError):
        signed_in = False
    if not signed_in:
        return f"`{setting}` {path} holds no Claude sign-in: run `CLAUDE_CONFIG_DIR={path} claude` there once and sign in"
    return None


TRAIN_DEFAULTS = {"batch": 5, "limit": 15}


def train_branch(cfg: dict) -> str:
    """The integration branch a program's items merge into, in every
    repository of the program that runs a train. Beside the item branches
    (`hl/<run>/<ID>`) and under a name of its own, so nothing that reads an
    item's branch takes it for one."""
    name = str(cfg["program"])
    plain = name and all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in name)
    if not plain or name.startswith(("-", ".")) or name.endswith((".", ".lock")) or ".." in name:
        raise Refusal(f"the program's name {name!r} cannot be part of a branch name: plain letters, digits, `.`, `_` and `-` only")
    return f"hl-train/{name}"


def batch_branch(cfg: dict, n: int) -> str:
    return f"hl-batch/{cfg['program']}/{int(n)}"


def train_settings(cfg: dict, repo: str) -> dict | None:
    """How a repository's train runs, or None when it has none: `batch`,
    the number of merged items at which a test is due, and `limit`, the
    number past which nothing more merges into it untested."""
    given = repo_facts(cfg, repo).get("train")
    if given is None:
        return None
    return {**TRAIN_DEFAULTS, **given}


def run_policy(cfg: dict, repo: str) -> dict:
    """The rules a new run in a repository starts under, and keeps: how its
    premises and reviews work, its bounds, and where its items merge."""
    settings = {**DEFAULTS, **cfg}
    chosen = {"premise": settings["premise"], "review": settings["review"], "bounds": settings["bounds"] or {}}
    if train_settings(settings, repo) is not None:
        chosen.update(merge="train", train={"branch": train_branch(settings), "item_checks": repo_facts(settings, repo).get("item_checks") or []})
    if settings["confirm"] != "full":
        chosen.update(confirm=settings["confirm"], confirm_bound=settings["confirm_bound"] or {})
    exemptable = repo_facts(settings, repo).get("exemptable") or []
    if exemptable:
        # Fixed into the run: what its manager may exempt is what the spec said when the run was created.
        chosen["exemptable"] = list(exemptable)
    return chosen


def repo_facts(cfg: dict, repo: str) -> dict:
    """What the program's spec says about a repository. Its name is matched
    as GitHub matches it, without regard to case, so a run started with
    `Owner/Repo` still finds the facts written for `owner/repo`."""
    wanted = (repo or "").lower()
    for name, facts in (cfg.get("repos") or {}).items():
        if name.lower() == wanted:
            return facts or {}
    return {}
