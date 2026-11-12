"""How long the program's items wait on their repository's CI, read at every save.

An item pays for its repository's checks on its pull request and, after the
merge, for the deploy its Verify line waits on. Both grow as the repository
grows, a little with every item, so nobody sees it happen. On a help-desk
program its required CI check took 9 minutes on 2026-10-23 and 64 on
2026-11-02: its tests ran one at a time on GitHub's hosted runners, and every
merge ran them twice, once on the pull request and again in the deploy. The operator asked
why items had slowed before any manager said so. `hl-flow save` gives these
facts at the end of every cycle, so the manager sees the trend and acts on it
(the prompt's "CI time").

Every check the pull request's head ran is counted, required or not. A check
the branch protection does not require still holds a runner, and a manager
whose merges wait for it pays for it in full: a container repository's `Full
pytest suite` took 45 minutes on 2026-11-02 and is not required, so a read of
the required checks alone gave that repository 1.3 minutes. Skipped checks are
left out: they ran nothing, and GitHub stamps some of them as ending before
they started.

For each repository, over its last `WINDOW` merged items that have a pull
request, the median of:

    checks   minutes from the first check's start to the last one's end, on
             the head that merged (`gh pr checks`), and the check that took
             longest
    verify   minutes from the merge to the item's Verify line passing

against the program's `ci_budget_minutes`. A repository counts as over only
with at least `MIN_ITEMS` measured: one slow merge (a runner queue, a retry)
is not a trend. Items on a train are left out: their full checks run on
batches, not on their own pull requests.

Each merged item's checks are read once and kept in its folder
(`ci-time.json`): a merged pull request's checks no longer change, so a save
asks GitHub only about items merged since the last. A kept read that counted
less than every check (`scope` is not `SCOPE`) is read again once. Nothing
here can fail a save; what could not be read is said under `errors`.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from . import items, program
from .common import Refusal, read_json, utc_now, write_json

WINDOW = 5
MIN_ITEMS = 3
CACHE = "ci-time.json"
# What a kept read counted. A read kept without it counted the required checks only.
SCOPE = "all"
READ_BUDGET_S = 30
# GitHub's stamp for a check that has not started or finished.
UNSET = "0001-01-01T00:00:00Z"


def _epoch(stamp) -> float | None:
    if not stamp or stamp == UNSET:
        return None
    try:
        return datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _minutes(seconds: float) -> float:
    return round(seconds / 60, 1)


def budget(cfg: dict) -> dict:
    """The program's budget in minutes, `checks` and `verify`, the kit's defaults filling what it leaves out."""
    given = cfg.get("ci_budget_minutes") or {}
    return {**program.DEFAULTS["ci_budget_minutes"], **{k: v for k, v in given.items() if v}}


def checks_time(checks: list[dict]) -> dict | None:
    """The wall time of a pull request's checks, from `gh pr checks --json
    name,bucket,startedAt,completedAt`, or None while any of them has not
    finished. Skipped checks are left out."""
    spans = []
    for c in checks:
        if c.get("bucket") == "skipping":
            continue
        start, end = _epoch(c.get("startedAt")), _epoch(c.get("completedAt"))
        if start is None or end is None or end < start:
            return None
        spans.append((c.get("name") or "?", start, end))
    if not spans:
        return None
    slowest = max(spans, key=lambda s: s[2] - s[1])
    return {
        "checks_minutes": _minutes(max(s[2] for s in spans) - min(s[1] for s in spans)),
        "slowest": slowest[0],
        "slowest_minutes": _minutes(slowest[2] - slowest[1]),
    }


def merged_items(root) -> dict[str, list[tuple[Path, dict]]]:
    """Each repository's merged items that have a pull request, newest merge
    first, leaving out runs on a train."""
    out: dict[str, list[tuple[Path, dict]]] = {}
    for run_json in sorted(Path(root).glob("runs/*/run.json")):
        run_dir = run_json.parent
        if run_dir.name.startswith("."):
            continue  # a run set aside (`runs/.void-*`): its items are counted where it was made again, if anywhere
        try:
            run = items.load_run(run_dir)
        except (Refusal, ValueError, OSError):
            continue
        if not run.get("repo") or items.on_train(run):
            continue
        for item_id in run.get("order") or []:
            try:
                item = items.load_item(run_dir, item_id)
            except (Refusal, ValueError, OSError):
                continue
            if item.get("merged_at") and (item.get("fields") or {}).get("pr"):
                out.setdefault(run["repo"], []).append((items.item_dir(run_dir, item_id), item))
    for found in out.values():
        found.sort(key=lambda pair: pair[1]["merged_at"], reverse=True)
    return out


def verify_minutes(item: dict) -> float | None:
    fields = item.get("fields") or {}
    if fields.get("verify") != "verified":
        return None
    start, end = _epoch(item.get("merged_at")), _epoch(fields.get("verify_at"))
    return _minutes(end - start) if start is not None and end is not None and end >= start else None


def item_checks(item_dir: Path, item: dict, slug: str, call, late: str | None = None) -> tuple[dict | None, str | None]:
    """An item's checks time, from its folder or else from GitHub, and what
    went wrong reading it. `late` says why GitHub is not asked any more."""
    fields = item.get("fields") or {}
    cache = item_dir / CACHE
    if cache.exists():
        try:
            kept = read_json(cache)
            if str(kept.get("pr")) == str(fields["pr"]) and kept.get("scope") == SCOPE:
                return kept, None
        except (ValueError, OSError):
            pass
    if late:
        return None, f"{slug} #{fields['pr']}: {late}"
    proc = call(["pr", "checks", str(fields["pr"]), "--repo", slug, "--json", "name,bucket,startedAt,completedAt"])
    text = (proc.stdout or "").strip()
    if not text:
        said = (proc.stderr or "").strip()
        if "no checks" in said.lower():
            return None, None
        return None, f"{slug} #{fields['pr']}: checks could not be read: {said[:160]}"
    try:
        timed = checks_time(json.loads(text))
    except ValueError:
        return None, f"{slug} #{fields['pr']}: gh gave no JSON: {text[:120]}"
    if timed is None:
        return None, None
    kept = {"pr": fields["pr"], "head_sha": fields.get("head_sha"), "scope": SCOPE, "read_at": utc_now(), **timed}
    write_json(cache, kept)
    return kept, None


def facts(cfg: dict, root, gh, budget_s: float = READ_BUDGET_S, clock=time.monotonic) -> dict:
    """Each repository's checks and verify minutes and whether they are over the program's budget."""
    limits = budget(cfg)
    out: dict = {"budget_minutes": limits, "window": WINDOW, "repos": {}, "over_budget": [], "errors": []}
    deadline = clock() + budget_s

    def call(args):
        try:
            return gh(args, timeout=max(deadline - clock(), 1))
        except TypeError:
            return gh(args)

    for slug, found in sorted(merged_items(root).items(), key=lambda kv: kv[0].lower()):
        recent = found[:WINDOW]
        checks, slowest, verify = [], [], []
        for item_dir, item in recent:
            minutes = verify_minutes(item)
            if minutes is not None:
                verify.append(minutes)
            late = f"not read within {int(budget_s)} s" if clock() >= deadline else None
            timed, error = item_checks(item_dir, item, slug, call, late=late)
            if error:
                out["errors"].append(error)
            if timed:
                checks.append(timed["checks_minutes"])
                slowest.append(timed["slowest"])
        newest_dir, newest = recent[0]
        kept = newest_dir / CACHE
        latest = {"id": newest["id"], "merged_at": newest["merged_at"], "verify_minutes": verify_minutes(newest)}
        if kept.exists():
            try:
                read = read_json(kept)
                if read.get("scope") == SCOPE:
                    latest.update({k: read.get(k) for k in ("checks_minutes", "slowest")})
            except (ValueError, OSError):
                pass
        repo = {
            "items": len(recent),
            "checks_minutes": round(statistics.median(checks), 1) if checks else None,
            "slowest": max(set(slowest), key=slowest.count) if slowest else None,
            "verify_minutes": round(statistics.median(verify), 1) if verify else None,
            "latest": latest,
        }
        measured = {"checks": len(checks), "verify": len(verify)}
        over = [k for k in ("checks", "verify") if measured[k] >= MIN_ITEMS and repo[f"{k}_minutes"] > limits[k]]
        repo["over"] = over
        out["repos"][slug] = repo
        if over:
            out["over_budget"].append(slug)
    return out


def facts_or_said(root, no_token: str | None = None, gh=None) -> dict:
    """`facts` for `hl-flow save`, which must never fail on it: whatever goes wrong is said in the answer instead."""
    try:
        cfg = program.load(root)
    except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
        reasons = exc.reasons if isinstance(exc, Refusal) else [str(exc)]
        return {"errors": [f"program.json could not be read: {'; '.join(reasons)}"[:300]]}
    if no_token:
        return {"budget_minutes": budget(cfg), "not_checked": f"not read, because the loop's own token could not be had: {no_token}"[:400]}
    try:
        if gh is None:
            from .outside import Gh, _gh_bin  # here: only the command line needs them

            gh = Gh(_gh_bin(root))
        return facts(cfg, root, gh)
    except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
        return {"budget_minutes": budget(cfg), "errors": [f"could not be read: {exc}"[:300]]}
