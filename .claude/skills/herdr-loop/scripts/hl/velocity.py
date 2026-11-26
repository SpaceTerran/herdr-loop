"""How long a passed item waits to merge, and what a merged item costs, read at every save.

The velocity review of the help-desk catalog program (2026-11-19) counted these by hand, from the run files and the Claude
transcripts on the host, and proposed changes to the kit and the program to be switched on one at a time. Each
change is kept or reverted on one of these numbers: the operator's note that switches it names the number, today's
value and the rule. `hl-flow save` gives them at the end of every cycle, so the manager reads a trial off the answer
and does not need a script of its own, and every later change can state its effect on the same numbers.

Over the program's last `WINDOW` merged items (every repository; runs on a train are left out, as for `ci_time`),
from the run files alone:

    merge_wait_minutes   median and p90 minutes from the item's last passing review (the grade's `read.at`) to
                         its merge: how long a passed item waits for the gate
    reviews_per_item     reviews (grades, delta confirms among them) per merged item
    rebuilt              the share of those items built more than once
    non_pass_reviews     their reviews that did not pass: how many, how many were INCONCLUSIVE, and how many
                         questioned a criterion (`criteria_in_question`), the review's "paperwork" verdicts
    verify               how their deploy checks came out (`verified`, `fixed_forward`, `verify_failed`, and
                         `unchecked` while none is recorded): a change that lets more through is kept on whether
                         it breaks more deploys

Where items came from, from each item's `**Origin.**` line (`backlog.ORIGINS`):

    origins              `merged`: the window's items by origin (`new`, `repair`, `audit`, `rule`, and `unrecorded`
                         for an item written without the line); `repair_share`, the repairs among those that say;
                         `written`: the items written since the window's oldest merge (an item is written when
                         its run is made), how many say nothing, and how many are a repair or an audit's finding;
                         and `found_per_100_merged`, those repairs and findings per 100 merged items in the window:
                         the defects found after their merge. It is null until an item written since says where
                         it came from, so a program that writes no origins reads as unmeasured, not as clean.
                         `repaired`: the window's items that a repair item names (`**Origin.** repair: <id>`), each
                         with the repairs that name it, so that a defect let through can be read back to its item

The page walks of the closing audits (`closing_audit: pages`), from every item's attempts and `**Origin.**` line:

    walks                walk attempts `started` (given back ones apart, as `voided`), how many `walked` (wrote
                         `walk-<n>.md`) and how many are `closed`, and the `repairs`: items whose origin is the
                         audit of a page walk (`audit: the page walk of <file>`)

The waits delivered as the manager's next message (`hl-wait --deliver`) since the window's oldest merge, from the
files they keep under each run's `waits/`:

    waits                `delivered` and `undelivered`: an event whose line could not be typed into the manager's
                         pane has `<file>.undelivered` beside it, since a detached wait's own answer goes nowhere

Over the last `FIRST_BUILDS` first builds that have a first review (any item, merged or not):

    first_builds         the share whose first review passed, the share of their items built again since, and
                         the efforts they ran at

Over the last `CONFIRM_MERGES` items merged on a delta confirm (the review that let each merge, its last counted
pass, confirmed a fix rather than reviewing the change in full; runs on a train left out):

    confirm_merges       how many, the oldest merge among them (`since`), how many confirmed a fix for red checks,
                         and how their deploy checks came out (`verified`, `fixed_forward`, `verify_failed`, and
                         `unchecked` while none is recorded): a delta confirm is kept on whether what it lets
                         through breaks a deploy more often than what a full review lets through

From the transcripts of the Claude sessions started since the window's oldest merge (an item's attempts name its
sessions; the manager's transcripts are the files beside the watchdog's manager session):

    builds               the p90 of a build session's peak context (input, cache read and cache write of its
                         largest request), the median of its turns (requests), the share of builds that compacted
                         their conversation at least once and the most compactions one build made
    tokens_per_item      weighted tokens per merged item, workers and manager apart: input + 1.25 x cache write
                         + 0.1 x cache read + 5 x output, the review's weighting (list prices relative to input)
    manager_per_item     the manager's commands (Bash calls) and turns per merged item, from the sessions that ran
                         the manager's model

A transcript is read whole once and its tally kept in `CACHE` in the program's root, with the file's size and
modification time: it is read again only when it has changed. Reading stops after `READ_BUDGET_S`; a session not
read in time is counted under `unread` (from its kept tally where one exists) and read at the next save. The kept
tallies are only those of the sessions this save looked at. Nothing here can fail a save: what could not be read is
said under `errors`, and a fact that has nothing to measure yet is null.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from . import herdr as herdr_mod
from . import backlog, items, program
from .common import Refusal, read_json, write_json

WINDOW = 40
FIRST_BUILDS = 40
CONFIRM_MERGES = 20
CACHE = "velocity-sessions.json"
# What a kept tally counted; a tally kept by another version is read again.
VERSION = 2
READ_BUDGET_S = 20
# A session younger than this may not have written its transcript yet: its absence is not kept.
MISSING_GRACE_S = 600
MAX_ERRORS = 5
WEIGHTS = {"input_tokens": 1.0, "cache_creation_input_tokens": 1.25, "cache_read_input_tokens": 0.1, "output_tokens": 5.0}
CONTEXT_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _epoch(stamp) -> float | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _p90(values: list) -> float | None:
    """Nearest rank: the smallest value at least 90% of the values are at or under."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 2) if whole else None


def _round(value, places=1):
    return None if value is None else round(value, places)


# ---------------------------------------------------------------- run files


def _runs(root):
    """Each run directory with its run record, leaving out runs set aside (`runs/.void-*`)."""
    for run_json in sorted(Path(root).glob("runs/*/run.json")):
        run_dir = run_json.parent
        if run_dir.name.startswith("."):
            continue
        try:
            yield run_dir, items.load_run(run_dir)
        except (Refusal, ValueError, OSError):
            continue


def _item_records(root):
    """Every item of every run: (folder, item, attempt log, run is on a train). An item whose files cannot be read
    is left out: it is not this fact's to report."""
    for run_dir, run in _runs(root):
        train = items.on_train(run)
        for item_id in run.get("order") or []:
            folder = items.item_dir(run_dir, item_id)
            try:
                item = items.load_item(run_dir, item_id)
                log = read_json(folder / "attempts.json").get("log") or []
            except (Refusal, ValueError, OSError):
                continue
            yield folder, item, log, train


def _verdict(folder: Path, grade: dict) -> str | None:
    """What a review said, from its read in the attempt log, else from `review-<n>.json`; None when neither says."""
    said = (grade.get("read") or {}).get("effective")
    if said:
        return said
    try:
        return read_json(folder / f"review-{grade.get('n')}.json").get("effective") or None
    except (ValueError, OSError):
        return None


def _review_record(folder: Path, grade: dict) -> dict:
    try:
        return read_json(folder / f"review-{grade.get('n')}.json")
    except (ValueError, OSError):
        return {}


def merged_window(records: list) -> list:
    """The last `WINDOW` merged items, newest merge first, leaving out runs on a train."""
    merged = [r for r in records if r[1].get("merged_at") and not r[3]]
    merged.sort(key=lambda r: r[1]["merged_at"], reverse=True)
    return merged[:WINDOW]


def merge_facts(window: list) -> dict:
    waits, reviews, confirms, rebuilt = [], 0, 0, 0
    non_pass = {"reviews": 0, "inconclusive": 0, "criterion_questioned": 0}
    verify = {"verified": 0, "fixed_forward": 0, "verify_failed": 0, "unchecked": 0}
    for folder, item, log, _train in window:
        verify[_verify_key((item.get("fields") or {}).get("verify"))] += 1
        merged = _epoch(item["merged_at"])
        grades = [e for e in log if e.get("phase") == "grade" and not e.get("voided")]
        reviews += len(grades)
        confirms += sum(1 for g in grades if g.get("kind") == items.CONFIRM)
        if any(e.get("phase") == "build" and (e.get("build") or 0) >= 2 for e in log):
            rebuilt += 1
        passes = []
        for grade in grades:
            verdict = _verdict(folder, grade)
            if verdict == "PASS":
                read_at = _epoch((grade.get("read") or {}).get("at"))
                if read_at is not None and merged is not None and read_at <= merged:
                    passes.append(read_at)
            elif verdict:
                non_pass["reviews"] += 1
                if verdict == "INCONCLUSIVE":
                    non_pass["inconclusive"] += 1
                if _review_record(folder, grade).get("criteria_in_question"):
                    non_pass["criterion_questioned"] += 1
        if passes:
            waits.append((merged - max(passes)) / 60)
    n = len(window)
    return {
        "merge_wait_minutes": {
            "median": _round(statistics.median(waits)) if waits else None,
            "p90": _round(_p90(waits)),
            "items": len(waits),
        },
        "reviews_per_item": _round(reviews / n, 2) if n else None,
        "delta_confirms": confirms,
        "rebuilt": _share(rebuilt, n),
        "non_pass_reviews": non_pass,
        "verify": verify,
    }


def _verify_key(said) -> str:
    """An item's recorded Verify (`items.VERIFY_RESULTS`) as the key it is counted under."""
    return said.replace("-", "_") if said in items.VERIFY_RESULTS else "unchecked"


def _origin(item: dict) -> str | None:
    kind = (item.get("origin") or {}).get("kind")
    return kind if kind in backlog.ORIGINS else None


def origin_facts(records: list, window: list, written: dict) -> dict:
    """Where the window's merged items came from, and the repairs and audit findings written since it opened.
    `written` is each run's directory and when the run was made, which is when its items were written."""
    merged = {kind: 0 for kind in backlog.ORIGINS}
    for _folder, item, _log, _train in window:
        kind = _origin(item)
        merged[kind or "unrecorded"] = merged.get(kind or "unrecorded", 0) + 1
    merged.setdefault("unrecorded", 0)
    since = _epoch(window[-1][1]["merged_at"]) if window else None
    later = {"items": 0, "unrecorded": 0, "repair": 0, "audit": 0}
    if since is not None:
        for folder, item, _log, _train in records:
            at = _epoch(written.get(str(folder.parent.parent)))
            if at is None or at < since:
                continue
            kind = _origin(item)
            later["items"] += 1
            if kind is None:
                later["unrecorded"] += 1
            elif kind in ("repair", "audit"):
                later[kind] += 1
    said = later["items"] - later["unrecorded"]
    return {
        "merged": merged,
        "repair_share": _share(merged["repair"], len(window) - merged["unrecorded"]),
        "written": later,
        "found_per_100_merged": round(100 * (later["repair"] + later["audit"]) / len(window), 1) if window and said else None,
        "repaired": _repaired(records, window),
    }


def _repaired(records: list, window: list) -> dict:
    """The window's items that a repair item names, each with the ids of the repairs that name it."""
    ids = {item.get("id") for _f, item, _l, _t in window}
    named: dict = {}
    for _folder, item, _log, _train in records:
        origin = item.get("origin") or {}
        if origin.get("kind") != "repair":
            continue
        for target in sorted(set(backlog.ID_RE.findall(origin.get("what") or "")) & ids):
            named.setdefault(target, []).append(item.get("id"))
    return {"items": len(named), "by": {target: sorted(by) for target, by in sorted(named.items())}}


# What a repair item found by a page walk says it came from (`stage.PAGE_WALK`).
WALK_ORIGIN = "the page walk"


def walk_facts(records: list) -> dict:
    """The page walks started on every item, how many walked and were closed, and the repair items they gave."""
    out = {"started": 0, "walked": 0, "closed": 0, "voided": 0, "repairs": 0}
    for folder, item, log, _train in records:
        for entry in log:
            if entry.get("phase") != "walk":
                continue
            if entry.get("voided"):
                out["voided"] += 1
                continue
            out["started"] += 1
            n = entry.get("n")
            if (folder / f"walk-{n}.md").exists():
                out["walked"] += 1
            try:
                closed = bool(read_json(herdr_mod.worker_path(folder, "walk", n)).get("closed_at"))
            except (ValueError, OSError):
                closed = False
            out["closed"] += closed
        origin = item.get("origin") or {}
        if origin.get("kind") == "audit" and (origin.get("what") or "").lower().startswith(WALK_ORIGIN):
            out["repairs"] += 1
    return out


# Beside an event `hl-wait --deliver` could not type into the manager's pane (`wait.deliver`).
UNDELIVERED = ".undelivered"


def wait_facts(root, since: float | None) -> dict:
    """The events `hl-wait --deliver` kept since `since` (every one when None), by whether they reached the manager.
    An event's file is named for when it was written (`<%Y%m%dT%H%M%SZ>-<pid>-<event>.json`)."""
    out = {"delivered": 0, "undelivered": 0}
    for run_dir, _run in _runs(root):
        for path in sorted((run_dir / "waits").glob("*.json")):
            try:
                at = datetime.strptime(path.name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if since is not None and at < since:
                continue
            out["undelivered" if path.with_suffix(UNDELIVERED).exists() else "delivered"] += 1
    return out


def first_build_facts(records: list) -> dict:
    """The last `FIRST_BUILDS` first builds that have a first review: how many of those reviews passed."""
    rows = []
    for folder, _item, log, _train in records:
        first = next((e for e in log if e.get("phase") == "build" and e.get("build") == 1 and e.get("engine") == "claude"), None)
        if not first:
            continue
        grade = next((e for e in log if e.get("phase") == "grade" and e.get("build") == 1 and not e.get("voided")), None)
        verdict = _verdict(folder, grade) if grade else None
        if verdict is None:
            continue
        again = any(e.get("phase") == "build" and (e.get("build") or 0) >= 2 for e in log)
        rows.append((str(first.get("at") or ""), first.get("effort"), verdict, again))
    rows.sort(reverse=True)
    rows = rows[:FIRST_BUILDS]
    efforts: dict = {}
    for row in rows:
        efforts[row[1] or "unknown"] = efforts.get(row[1] or "unknown", 0) + 1
    return {
        "builds": len(rows),
        "since": rows[-1][0] if rows else None,
        "first_review_passed": _share(sum(1 for r in rows if r[2] == "PASS"), len(rows)),
        "rebuilt": _share(sum(1 for r in rows if r[3]), len(rows)),
        "efforts": efforts,
    }


def confirm_merge_facts(records: list) -> dict:
    """The last `CONFIRM_MERGES` items merged on a delta confirm, and what their deploy checks gave."""
    rows = []
    for folder, item, log, train in records:
        if not item.get("merged_at") or train:
            continue
        passed = next((e for e in reversed(log) if e.get("phase") == "grade" and not e.get("voided") and _verdict(folder, e) == "PASS"), None)
        if passed is None or passed.get("kind") != items.CONFIRM:
            continue
        rows.append((str(item["merged_at"]), (item.get("fields") or {}).get("verify"), passed.get("red_checks") is not None))
    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[:CONFIRM_MERGES]
    return {
        "items": len(rows),
        "since": rows[-1][0] if rows else None,
        "red_checks": sum(1 for r in rows if r[2]),
        "verified": sum(1 for r in rows if r[1] == "verified"),
        "fixed_forward": sum(1 for r in rows if r[1] == "fixed-forward"),
        "verify_failed": sum(1 for r in rows if r[1] == "verify-failed"),
        "unchecked": sum(1 for r in rows if r[1] not in items.VERIFY_RESULTS),
    }


# ---------------------------------------------------------------- transcripts


def tally(path: Path) -> dict:
    """One transcript's requests: each counted once by its message id (a streamed reply is written more than
    once, the last with the most output), with its tokens, its context size and the Bash calls it made, and the
    turns, Bash calls and weighted tokens of each UTC hour, so a window can start at any hour."""
    requests: dict = {}
    bash: dict = {}
    model = None
    compactions = 0
    with open(path, "rb") as fh:
        for raw in fh:
            if b'"compact_boundary"' in raw:
                try:
                    marker = json.loads(raw)
                except ValueError:
                    marker = None
                # Claude Code writes one such line each time it compacts the conversation.
                if isinstance(marker, dict) and marker.get("type") == "system" and marker.get("subtype") == "compact_boundary" and not marker.get("isSidechain"):
                    compactions += 1
                continue
            if b'"assistant"' not in raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("type") != "assistant" or record.get("isSidechain"):
                continue
            message = record.get("message") or {}
            usage = message.get("usage") or {}
            key = message.get("id") or record.get("uuid")
            hour = str(record.get("timestamp") or "")[:13]
            model = message.get("model") or model
            out = int(usage.get("output_tokens") or 0)
            kept = requests.get(key)
            if kept is None or out >= kept["out"]:
                requests[key] = {
                    "hour": kept["hour"] if kept else hour,
                    "out": out,
                    "context": sum(int(usage.get(k) or 0) for k in CONTEXT_KEYS),
                    "weighted": sum(w * int(usage.get(k) or 0) for k, w in WEIGHTS.items()),
                }
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
                    bash.setdefault(block.get("id"), requests[key]["hour"])
    hours: dict = {}
    for request in requests.values():
        bucket = hours.setdefault(request["hour"], [0, 0, 0])
        bucket[0] += 1
        bucket[2] += request["weighted"]
    for hour in bash.values():
        hours.setdefault(hour, [0, 0, 0])[1] += 1
    return {
        "model": model,
        "turns": len(requests),
        "peak": max((r["context"] for r in requests.values()), default=0),
        "bash": len(bash),
        "compactions": compactions,
        "weighted": round(sum(r["weighted"] for r in requests.values())),
        "hours": {h: [b[0], b[1], round(b[2])] for h, b in sorted(hours.items())},
    }


class Reader:
    """Tallies of transcripts, from `CACHE` where the file has not changed, else read now while the budget lasts."""

    def __init__(self, root, budget_s: float = READ_BUDGET_S, clock=time.monotonic, now=time.time, find=None):
        self.path = Path(root) / CACHE
        try:
            kept = read_json(self.path)
            self.kept = (kept.get("sessions") or {}) if kept.get("version") == VERSION else {}
        except (ValueError, OSError):
            self.kept = {}
        self.used: dict = {}
        self.deadline = clock() + budget_s
        self.clock, self.now = clock, now
        self.find = find or herdr_mod.claude_transcripts
        self.unread = 0
        self.errors: list = []

    def _file(self, key: str, path: Path) -> dict | None:
        try:
            st = path.stat()
        except OSError as exc:
            self._error(f"{path.name}: {exc.strerror or exc}")
            return None
        kept = self.kept.get(key)
        if kept and kept.get("path") == str(path) and kept.get("size") == st.st_size and kept.get("mtime") == int(st.st_mtime):
            self.used[key] = kept
            return kept
        if self.clock() >= self.deadline:
            self.unread += 1
            if kept and kept.get("path") == str(path):
                self.used[key] = kept
                return kept
            return None
        try:
            fresh = {"path": str(path), "size": st.st_size, "mtime": int(st.st_mtime), **tally(path)}
        except OSError as exc:
            self._error(f"{path.name}: {exc.strerror or exc}")
            return None
        self.used[key] = fresh
        return fresh

    def session(self, session: str, started_at: float | None) -> tuple[dict | None, list[dict]]:
        """A worker session's tally and those of its sub-agents: (None, []) when it has no transcript, and the
        first None when its own transcript was not read in time. Where the transcript is is kept, since finding
        it searches every Claude home; its sub-agents are listed again each time, as a session may start more."""
        marker = f"session:{session}"
        kept = self.kept.get(marker) or {}
        if kept.get("missing"):
            self.used[marker] = kept
            return None, []
        main = Path(kept["main"]) if kept.get("main") else None
        if main is None or not main.exists():
            if self.clock() >= self.deadline:
                self.unread += 1
                return None, []
            found = self.find(session)
            if not found:
                # A session just started may not have written its transcript yet; one long gone never will.
                if started_at is not None and self.now() - started_at > MISSING_GRACE_S:
                    self.used[marker] = {"missing": True}
                return None, []
            main = Path(found[0])
        self.used[marker] = {"main": str(main)}
        subs = sorted((main.parent / session / "subagents").glob("*.jsonl"))
        return self._file(f"file:{main}", main), [t for t in (self._file(f"file:{p}", p) for p in subs) if t]

    def directory(self, folder: Path, since: float, leave_out=frozenset()) -> list[tuple[dict, bool]]:
        """The tallies of the transcripts in one directory written since `since`, each with whether it is a
        sub-agent's (kept beside its session under `<session>/subagents/`). Sessions in `leave_out` are not read."""
        out = []
        for path in sorted(folder.glob("*.jsonl")) + sorted(folder.glob("*/subagents/*.jsonl")):
            sub = path.parent.name == "subagents"
            if (path.parent.parent.name if sub else path.stem) in leave_out:
                continue
            try:
                if path.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            found = self._file(f"file:{path}", path)
            if found:
                out.append((found, sub))
        return out

    def _error(self, text: str) -> None:
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(text[:200])

    def keep(self) -> None:
        write_json(self.path, {"version": VERSION, "sessions": self.used})


def _hour_of(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H")


def _manager_dir(root) -> Path | None:
    """Where the manager's transcripts are: beside the transcript of the session the watchdog started."""
    path = Path(root) / "watchdog.json"
    if not path.exists():
        return None
    session = (read_json(path).get("manager") or {}).get("session")
    transcript = herdr_mod.claude_transcript(session) if session else None
    return transcript.parent if transcript else None


def session_facts(cfg: dict, root, records: list, since: float, merged: int, reader: Reader, manager_dir=None) -> dict:
    peaks, turns, compactions, worker_tokens, sessions = [], [], [], 0, 0
    workers = {e.get("session") for _f, _i, log, _t in records for e in log if e.get("session")}
    counted: set = set()
    for _folder, _item, log, _train in records:
        for entry in log:
            session = entry.get("session")
            started = _epoch(entry.get("at"))
            if not session or session in counted or entry.get("engine") != "claude" or started is None or started < since:
                continue
            counted.add(session)
            main, subs = reader.session(session, started)
            if main is None and not subs:
                continue
            sessions += 1
            worker_tokens += sum(t["weighted"] for t in ([main] if main else []) + subs)
            if entry.get("phase") == "build" and main is not None:
                peaks.append(main["peak"])
                turns.append(main["turns"])
                compactions.append(main.get("compactions") or 0)
    from_hour = _hour_of(since)
    manager_tokens = manager_bash = manager_turns = 0
    model = str(cfg.get("manager_model") or "").split("[")[0]
    folder = manager_dir if manager_dir is not None else _manager_dir(root)
    for found, sub in reader.directory(folder, since, leave_out=workers) if folder else []:
        runs_manager_model = not sub and bool(model) and str(found.get("model") or "").startswith(model)
        for hour, (n_turns, n_bash, weighted) in found["hours"].items():
            if hour < from_hour:
                continue
            manager_tokens += weighted
            if runs_manager_model:
                manager_turns += n_turns
                manager_bash += n_bash
    per = (lambda v: round(v / merged)) if merged else (lambda v: None)
    per_1 = (lambda v: round(v / merged, 1)) if merged else (lambda v: None)
    return {
        "builds": {
            "sessions": len(peaks),
            "peak_context_p90": _p90(peaks),
            "turns_median": statistics.median(turns) if turns else None,
            # How many builds compacted their conversation at least once, and the most times one did: a build that
            # compacts again and again is one whose files refill the window straight after (`builder_autocompact`).
            "compacted": _share(sum(1 for c in compactions if c), len(compactions)),
            "compactions_max": max(compactions, default=None),
        },
        "tokens_per_item": {
            "workers": per(worker_tokens),
            "manager": per(manager_tokens) if folder else None,
            "total": per(worker_tokens + manager_tokens) if folder else None,
            "worker_sessions": sessions,
        },
        "manager_per_item": {"commands": per_1(manager_bash), "turns": per_1(manager_turns)} if folder else None,
    }


# ---------------------------------------------------------------- the fact


def facts(cfg: dict, root, budget_s: float = READ_BUDGET_S, clock=time.monotonic, now=time.time, find=None, manager_dir=None) -> dict:
    records = list(_item_records(root))
    window = merged_window(records)
    out: dict = {"window": {"merged": len(window), "since": window[-1][1]["merged_at"] if window else None}}
    out.update(merge_facts(window))
    out["first_builds"] = first_build_facts(records)
    out["confirm_merges"] = confirm_merge_facts(records)
    out["origins"] = origin_facts(records, window, {str(run_dir): run.get("created") for run_dir, run in _runs(root)})
    out["walks"] = walk_facts(records)
    errors: list = []
    since = _epoch(out["window"]["since"])
    out["waits"] = wait_facts(root, since)
    if since is not None:
        reader = Reader(root, budget_s=budget_s, clock=clock, now=now, find=find)
        out.update(session_facts(cfg, root, records, since, len(window), reader, manager_dir=manager_dir))
        out["unread"] = reader.unread
        errors += reader.errors
        try:
            reader.keep()
        except OSError as exc:
            errors.append(f"{CACHE} could not be written: {exc.strerror or exc}"[:200])
    out["errors"] = errors
    return out


def facts_or_said(root) -> dict:
    """`facts` for `hl-flow save`, which must never fail on it: whatever goes wrong is said in the answer instead."""
    try:
        cfg = program.load(root)
    except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
        reasons = exc.reasons if isinstance(exc, Refusal) else [str(exc)]
        return {"errors": [f"program.json could not be read: {'; '.join(reasons)}"[:300]]}
    try:
        return facts(cfg, root)
    except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
        return {"errors": [f"could not be read: {exc}"[:300]]}
