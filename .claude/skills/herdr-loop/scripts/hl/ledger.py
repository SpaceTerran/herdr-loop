"""The parts of a program's ledger that are read from the run directories.

The manager's ledger is what the next manager session knows. In the first
program it was one file the manager rewrote by hand at every save and read
whole at every start: 64 KB after five hours, 71 % of it tables that repeat
what the run directories already hold and proposals copied out of result
files a line at a time. Those parts are generated here instead, into files
of their own that a starting session does not read:

    LEDGER-ITEMS.md   runs, items and engines, from `runs/*`
    FOLLOW-UPS.md     what builders proposed and reviewers found, from the
                      result files: the manager has a sub-agent read it
                      for defects to place, the rest is the operator's
                      to file or drop

What is left in LEDGER.md is what only the manager can write: where things
stand, decisions that are the operator's, gates, and lessons.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

from . import contractfmt, items, verdict
from .common import read_json

PROPOSED_HEADING = "## Proposed items"
NOTHING = re.compile(r"^(none|nothing|n/a|-)\.?$", re.IGNORECASE)


def _epoch(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _minutes(start: str | None, end: str | None) -> float | None:
    a, b = _epoch(start), _epoch(end)
    return round((b - a) / 60, 1) if a is not None and b is not None and b >= a else None


def _cell(value) -> str:
    """A table cell: one line, its pipes escaped. A carriage return breaks a line as a newline does."""
    return "" if value is None else re.sub(r"\r\n|\r|\n", " ", str(value).replace("|", "\\|"))


def _table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def run_dirs(root) -> list[Path]:
    """Every run of the program. A directory whose name starts with a dot is one set aside (a run voided by hand,
    `runs/.void-*`) and is no run: on the help-desk catalog program (2026-11-11) two such were counted as waiting
    in `idle`."""
    return sorted(p.parent for p in Path(root).glob("runs/*/run.json") if not p.parent.name.startswith("."))


ITEM_FILE = "-ITEMS.md"
GENERATED = ("LEDGER-ITEMS.md",)
# A ledger line that says a backlog file is not to be run: `held: X-ITEMS.md`, `dropped: …`, `superseded: … by …`.
# The file named first after the marker is the one it accounts for.
NOT_RUN = re.compile(r"\b(?i:held|dropped|superseded):\s*`?([\w.-]+-ITEMS\.md)\b")
# In the ledger's archive only an end counts: a hold is a decision to look again, and lasts while its row is live.
ENDED = re.compile(r"\b(?i:dropped|superseded):\s*`?([\w.-]+-ITEMS\.md)\b")


def unstarted_files(root, ledger_file, archive_file=None) -> dict:
    """The backlog files beside the ledger that no run carries and that the ledger does not say are held, dropped
    or superseded (a line `held: <file>`, `dropped: <file>` or `superseded: <file> by <other>`). `dropped` and
    `superseded` count in the ledger or its archive; `held` only in the ledger, so once an archive moves a hold's
    row the file is listed again, to be started or held again with a row of today's.

    A run decided in the ledger and never initialised went unseen twice on the help-desk catalog program: `hd-nav`
    (its own Decisions row) and `hd-catalog-design` with `hd-request-view`, decided 2026-11-03 and found by the operator
    eight days later; a sweep then found five more files with no run and no reason. A fact for the manager:
    whether such a file should run is its to decide."""
    task = Path(ledger_file).parent
    files = sorted(p.name for p in task.glob("*" + ITEM_FILE) if p.is_file() and p.name not in GENERATED)
    carried, named, errors = set(), set(), []
    for run_dir in run_dirs(root):
        try:
            carried.update(Path(r["path"]).name for r in items.load_run(run_dir).get("files") or [] if r.get("path"))
        except Exception as exc:  # noqa: BLE001 - a fact, never fatal
            errors.append(f"{run_dir.name}: {exc}"[:200])
    for path, marker in ((Path(ledger_file), NOT_RUN), (Path(archive_file) if archive_file else None, ENDED)):
        if path is None or not path.exists():
            continue
        try:
            named.update(marker.findall(path.read_text()))
        except OSError as exc:
            errors.append(f"{path.name}: {exc}"[:200])
    out: dict = {"item_files": len(files), "files": [f for f in files if f not in carried and f not in named]}
    if errors:
        out["errors"] = errors
    return out


def idle_streams(root, streams, at: str) -> dict:
    """The streams with no item in progress at `at`: since when, and how
    many items still wait in their runs.

    A stream is one clone that works one item at a time, so a stream with
    nothing in progress is capacity nobody is using. On the help-desk catalog
    program (2026-11-07) three streams had an item in flight 12 %, 13 % and 41 % of
    the day while 27 defects sat noted and unplaced, and nothing the manager
    read at a save said so. `since` is the last time anything of the stream
    changed (an item merged, failed, was parked or skipped, or a run was
    made); a stream between a merge and its next item's first worker is
    listed too. A run that names no stream of the program cannot be counted
    for one: it is named under `runs_outside_streams` while it has items
    to work, so the list is never read as complete when it is not. This is
    a fact for the manager: nothing here decides what an idle stream should
    do."""
    seen = {name: {"in_progress": 0, "pending": 0, "last": None} for name in streams}
    outside = []
    for run_dir in run_dirs(root):
        run = items.load_run(run_dir)
        statuses = [items.load_item(run_dir, item_id)["status"] for item_id in run["order"]]
        stream = seen.get(run.get("stream"))
        if stream is None:
            if any(s in ("in_progress", "pending") for s in statuses):
                outside.append(run["run_id"])
            continue
        for status in ("in_progress", "pending"):
            stream[status] += statuses.count(status)
        for stamp in [run.get("created")] + [e.get("at") for e in run.get("events", [])]:
            if _epoch(stamp) is not None and (stream["last"] is None or _epoch(stamp) > _epoch(stream["last"])):
                stream["last"] = stamp
    listed = []
    for name, stream in seen.items():
        if stream["in_progress"]:
            continue
        minutes = _minutes(stream["last"], at)
        listed.append({"stream": name, "since": stream["last"], "minutes": None if minutes is None else int(minutes), "pending": stream["pending"]})
    out: dict = {"streams": listed}
    if outside:
        out["runs_outside_streams"] = outside
    return out


def item_record(run_dir, item_id: str) -> dict:
    """One item as the ledger shows it: how it was routed, who built it and
    at what effort, how its first review went, and what it took."""
    run = items.load_run(run_dir)
    item = items.load_item(run_dir, item_id)
    d = items.item_dir(run_dir, item_id)
    log = [e for e in items.load_attempts(run_dir, item_id)["log"] if not e.get("voided")]
    style = items.policy(run)["review"]
    builds = [e for e in log if e["phase"] == "build"]
    reviews = [e for e in log if e["phase"] == "grade"]
    words = []
    for e in reviews:
        checked = verdict.check(d, e["n"], style=style)
        word = checked["effective"] or ("unusable" if (d / f"verdict-{e['n']}.json").exists() else "none")
        # A delta confirm was given the fix made since the failed review before it, not the whole change.
        words.append(f"{word} (confirm)" if e.get("kind") == items.CONFIRM else word)
    merged = read_json(d / "merge.json") if (d / "merge.json").exists() else {}
    merged_at = merged.get("at") if merged.get("state") == "MERGED" else None
    route = item.get("route") or {}
    return {
        "id": item_id,
        "run": run["run_id"],
        "status": item["status"],
        "floor": item.get("floor") or [],
        "premise": route.get("premise"),
        "look": route.get("look"),
        "builders": [e.get("engine", "?") + (f"@{e['effort']}" if e.get("effort") else "") for e in builds],
        "first_builder": (builds[0].get("engine", "?") + (f"@{builds[0]['effort']}" if builds[0].get("effort") else "")) if builds else None,
        "reviews": words,
        "first_review": words[0] if words else None,
        "premise_rounds": sum(1 for e in log if e["phase"] == "agree"),
        "corrections": sum(1 for e in log if e["phase"] == "correction"),
        "minutes": _minutes(item.get("started_at"), merged_at),
        "pr": item["fields"].get("pr"),
        # For a run on a train: the batch that took the merge to the base, or "no" while it waits on the train.
        "promoted": (item["fields"].get("promoted") or ("no" if item["status"] == "merged" else None)) if items.on_train(run) else None,
        "verify": item["fields"].get("verify") if item.get("verify_command") else "none",
        "note": item.get("note"),
    }


def records(root) -> list[dict]:
    out = []
    for run_dir in run_dirs(root):
        for item_id in items.load_run(run_dir)["order"]:
            out.append(item_record(run_dir, item_id))
    return out


def items_text(root, program: str, at: str) -> str:
    """LEDGER-ITEMS.md, whole."""
    runs = []
    for run_dir in run_dirs(root):
        run = items.load_run(run_dir)
        counts = items.next_item(run_dir)["counts"]
        state = items.next_item(run_dir)["state"]
        policy = items.policy(run)
        runs.append(
            [run["run_id"], run.get("stream"), run.get("work"), f"{policy['premise']}, {policy['review']}" + (", train" if policy["merge"] == "train" else "") + (", repair" if run.get("repair") else ""), state]
            + [counts[k] for k in ("merged", "in_progress", "pending", "failed", "parked", "skipped")]
        )
    all_items = records(root)
    started = [r for r in all_items if r["status"] != "pending"]
    rows = [
        [
            r["id"],
            r["run"],
            r["status"] + (" (on the train)" if r["promoted"] == "no" else f" (batch {r['promoted']})" if r["promoted"] else ""),
            ("independent" if r["premise"] == "independent" else "builder" if r["premise"] else "") + (", look" if r["look"] else ""),
            " → ".join(r["builders"]),
            " → ".join(r["reviews"]),
            r["corrections"] or "",
            r["minutes"],
            f"#{r['pr']}" if r["pr"] else "",
            r["verify"],
            r["note"] if r["status"] in ("failed", "parked", "skipped") else "",
        ]
        for r in started
    ]
    by_engine: dict[str, list[dict]] = {}
    for r in started:
        if r["first_builder"]:
            by_engine.setdefault(r["first_builder"], []).append(r)
    engines = []
    for name in sorted(by_engine):
        group = by_engine[name]
        reviewed = [r for r in group if r["first_review"]]
        merged = [r for r in group if r["status"] == "merged"]
        minutes = [r["minutes"] for r in merged if r["minutes"] is not None]
        engines.append(
            [
                name,
                len(group),
                f"{sum(1 for r in reviewed if r['first_review'] == 'PASS')} of {len(reviewed)}",
                len(merged),
                statistics.median(minutes) if minutes else None,
                round(statistics.mean(len(r["builders"]) for r in merged), 2) if merged else None,
            ]
        )
    return "\n".join(
        [
            f"# Ledger tables: {program}",
            "",
            f"Generated from the run directories at {at} by `hl-flow save`. Do not edit: the next save writes it again.",
            "",
            "## Runs",
            "",
            _table(["Run", "Stream", "Work", "Policy", "State", "Merged", "In progress", "Pending", "Failed", "Parked", "Skipped"], runs),
            "",
            "## Items",
            "",
            "Every item that has started. Builds and reviews are in order; minutes run from the first phase to the merge.",
            "",
            _table(["Item", "Run", "Status", "Route", "Builds", "Reviews", "Corrections", "Minutes", "PR", "Verify", "Note"], rows),
            "",
            "## Engines",
            "",
            "By the builder and effort of an item's first build.",
            "",
            _table(["First build", "Items", "First review passed", "Merged", "Median minutes", "Mean builds to merge"], engines),
            "",
            *_outside_section(root),
        ]
    )


def _outside_section(root) -> list[str]:
    """The pull requests from outside the program it merged (`hl-merge --outside`), when there are any."""
    try:
        from . import outside  # here, not at the top: outside.py is built on merge.py

        merged = outside.records(root)
        if not merged:
            return []
        word = {"MERGED": None, "INTENT": "begun, no record", "UNREADABLE": "unreadable"}
        rows = [
            [
                f"#{r['pr']}" if r.get("pr") is not None else "",
                r.get("repo"),
                r.get("run_id"),
                r.get("branch"),
                r.get("at"),
                str(r.get("merge_sha") or "")[:12],
                word.get(r.get("state")) or r.get("verify") or "not run",
                "shared login accepted" if r.get("shared_login_accepted") else "",
            ]
            for r in merged
        ]
    except Exception as exc:  # noqa: BLE001 - a generated table never fails a save
        return ["## Outside pull requests", "", f"Could not be read: {exc}"[:300], ""]
    return [
        "## Outside pull requests",
        "",
        "Labeled `rollout-ready` by a steward and merged with this program's own merges.",
        "",
        _table(["PR", "Repository", "Run", "Branch", "Merged", "Commit", "Deploy check", "Vetting"], rows),
        "",
    ]


def _proposed(report: Path) -> list[str]:
    try:
        body = contractfmt.section(report.read_text(), PROPOSED_HEADING)
    except (OSError, contractfmt.ContractError):
        return []
    out = []
    for raw in body.splitlines():
        line = raw.strip().lstrip("-*").strip()
        if line and not NOTHING.match(line):
            out.append(line)
    return out


def followups(root) -> list[dict]:
    """What builders proposed and reviewers found, one row each, in run and item order. `report` is the file the
    row came from, relative to the loop root; `live` says its item is still in progress, so the file may yet
    change."""
    out = []
    root = Path(root)
    for run_dir in run_dirs(root):
        run = items.load_run(run_dir)
        for item_id in run["order"]:
            d = items.item_dir(run_dir, item_id)
            try:
                live = items.load_item(run_dir, item_id)["status"] == "in_progress"
            except Exception:  # noqa: BLE001 - an unreadable item is not one at work
                live = False
            for report in sorted(d.glob("build-*.md")):
                if not re.fullmatch(r"build-\d+\.md", report.name):
                    continue
                for line in _proposed(report):
                    out.append({"item": item_id, "run": run["run_id"], "from": report.stem, "report": _relative(report, root), "live": live, "kind": "proposed", "severity": "", "what": line})
            for path in sorted(d.glob("verdict-*.json")):
                try:
                    data = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                for finding in data.get("findings") or [] if isinstance(data, dict) else []:
                    if isinstance(finding, dict) and finding.get("summary"):
                        out.append(
                            {
                                "item": item_id,
                                "run": run["run_id"],
                                "from": path.stem,
                                "report": _relative(path, root),
                                "live": live,
                                "kind": finding.get("kind") or "finding",
                                "severity": finding.get("severity") or "",
                                "what": str(finding["summary"]),
                            }
                        )
    return out


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def followup_table_rows(rows: list[dict]) -> list[list[str]]:
    """The follow-ups table's rows. The same remark made about several items is listed once, with every item that carries it."""
    grouped: dict[str, dict] = {}
    for row in rows:
        key = " ".join(row["what"].lower().split()).rstrip(".")
        entry = grouped.setdefault(key, {"what": row["what"], "kinds": [], "severity": "", "where": []})
        if row["kind"] not in entry["kinds"]:
            entry["kinds"].append(row["kind"])
        entry["severity"] = entry["severity"] or row["severity"]
        entry["where"].append(f"{row['item']} {row['from']}")
    return [[", ".join(e["kinds"]), e["severity"], e["what"], ", ".join(e["where"])] for e in grouped.values()]


FOLLOWUPS_INTRO = (
    "What builders proposed and reviewers found that no item acted on. The manager has a sub-agent read it for defects to place; the rest is for the operator to file or drop. Nothing here blocks a merge."
)
FOLLOWUPS_ARCHIVE_INTRO = (
    "Follow-ups that `hl-flow archive` moved out of `FOLLOW-UPS.md` once a sub-agent had read them for defects to place, one section for each archive. "
    "Each archive adds its section and changes nothing already here; do not edit. History: read a row with `grep -n`, never the file whole."
)


def followups_text(root, program: str, at: str, rows: list[dict] | None = None) -> str:
    """FOLLOW-UPS.md, whole. `rows` is the follow-ups to list (default: all of them)."""
    rows = followups(root) if rows is None else rows
    return "\n".join(
        [
            f"# Follow-ups: {program}",
            "",
            f"Generated from the build reports and verdicts at {at} by `hl-flow save`. Do not edit: the next save writes it again.",
            "",
            FOLLOWUPS_INTRO,
            "A remark made about several items is listed once.",
            "",
            _table(["Kind", "Severity", "What", "From"], followup_table_rows(rows)),
            "",
        ]
    )


def followups_archive_text(existing: str | None, program: str, at: str, rows: list[dict], newline: str = "\n") -> str:
    """FOLLOW-UPS-ARCHIVE.md with a section added for the follow-ups an archive moves at `at`. What it held stays
    as it was, byte for byte but for the line breaks at its end: a report's rows are not read again once they have
    moved, so a run voided by hand afterwards loses none of them. A file of blank lines only is started afresh, with
    its title. The section's lines end with `newline`."""
    if existing and existing.strip():
        head = existing.rstrip("\r\n")
    else:
        head = newline.join([f"# Follow-ups archive: {program}", "", FOLLOWUPS_ARCHIVE_INTRO, "A remark made about several items is listed once in its section."])
    table = _table(["Kind", "Severity", "What", "From"], followup_table_rows(rows)).split("\n")
    return head + newline + newline.join(["", f"## Archived {at}", "", *table, ""])
