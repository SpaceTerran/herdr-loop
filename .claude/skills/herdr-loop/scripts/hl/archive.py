"""Moving a program's settled history out of its ledger.

A ledger only grows. On the help-desk catalog program after fifteen days (2026-11-11) `LEDGER.md` was 533 KB,
381 KB of it the Decisions table (437 rows), and `FOLLOW-UPS.md` was 635 KB, written whole from every report the program had
ever had; the manager had also started a `NOW-ARCHIVE.md` of its own (251 KB). A starting session reads only
"Now" and Placements, but every sweep for defects noted and never placed read Decisions, Lessons and
FOLLOW-UPS.md again, most of it placed or dropped long before, and every `grep -n` on Decisions came back longer.

`hl-flow archive` moves rows out by a rule that needs no judgement, into files beside the ledger that stay in
the ledger branch and are read a row at a time:

    LEDGER-ARCHIVE.md      every Decisions row but the newest KEEP_DECISIONS (by number), and every row of a
                           gate whose last row says it is settled (`clocks.SETTLED`, the rule `clocks` uses)
    FOLLOW-UPS-ARCHIVE.md  the follow-ups of every report whose item is not in progress; which reports moved
                           is kept in FOLLOW-UPS-ARCHIVED.json, and `save` leaves them out of FOLLOW-UPS.md

Decisions move by position, not by what their State column says: on that ledger the column held about a hundred
spellings, many no longer true ("building", "drafting"), and none a script could read as settled. The ledger's
history is Decisions by design; what is live is in "Now". Lessons, Placements, "Now" and every other section
stay: they are prose the manager writes and orders as it likes.

Nothing is lost. The ledger is read once and the move worked out from that read; before anything is written,
the ledger must come out as it was but for the rows that move and the pointer lines, and the rows of the ledger
and the archive together must be the same rows. Then every file is written or none is, each file that gains rows
before the one that loses them, and a ledger that changed since it was read is not written over. A row broken
over two lines (the next line text that is no row, or a row with an empty first cell) never moves: its second
half would stay behind. Nor does a gate whose last row reads more than one way (`clocks.gate_unclear`); every
row the clocks fact reads is read, in the same lines, so a gate that fact holds open never moves.
FOLLOW-UPS-ARCHIVE.md is only added to. Every line keeps the break it was written with, and a line the archive
adds takes the file's: only the rows that move leave a file.

Nothing leaves unread. `--dry-run --out <file>` writes what would move for the sub-agents that sweep it for
defects to place (the manager prompt), and the move is given that file back (`--swept <file>`) and takes only
the rows it lists and the reports it lists, each with a digest of what it said. A sweep of a long ledger takes a
while: a Decision written meanwhile pushes one more row past the newest KEEP_DECISIONS, an item that finishes
frees its follow-ups, a verdict is written again. Those stay for the next archive. A report that changes after
it moved is listed in FOLLOW-UPS.md again until another sweep has read it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

from . import clocks, ledger

ARCHIVE = "LEDGER-ARCHIVE.md"
FOLLOWUPS = "FOLLOW-UPS.md"
FOLLOWUPS_ARCHIVE = "FOLLOW-UPS-ARCHIVE.md"
ARCHIVED_REPORTS = "FOLLOW-UPS-ARCHIVED.json"
KEEP_DECISIONS = 40
SECTIONS = ("Gates", "Decisions")
POINTER = "Older rows: `LEDGER-ARCHIVE.md`"
DECISION_ROW = re.compile(r"^\|\s*(\d+)\s*\|")
SEPARATOR = re.compile(r"^\|(\s*:?-+:?\s*\|)*\s*:?-+:?\s*\|?\s*$")  # `|---|---|`, its last pipe optional
HEADING = re.compile(r"^#{1,6}(\s|$)")
DEFAULT_HEADERS = {
    "Gates": ["| Gate | Before | Result | When | How it was checked |", "|---|---|---|---|---|"],
    "Decisions": ["| # | Item | Question | Recommendation | Asked | State |", "|---|---|---|---|---|---|"],
}


class ArchiveError(RuntimeError):
    pass


class UndoError(ArchiveError):
    """A write failed and a file it had already written could not be put back."""


# ---------------------------------------------------------------- reading a ledger


_section = clocks.section


def _table_block(lines: list[str], start: int, end: int) -> tuple[int, int] | None:
    """The first run of table lines in lines[start:end], as (first, end)."""
    first = next((i for i in range(start, end) if lines[i].startswith("|")), None)
    if first is None:
        return None
    last = first
    while last < end and lines[last].startswith("|"):
        last += 1
    return first, last


def _header(lines: list[str], block: tuple[int, int] | None) -> list[str]:
    if block is None:
        return []
    first, end = block
    if first + 1 < end and SEPARATOR.match(lines[first + 1]):
        return lines[first : first + 2]
    return []


def _gates_header(lines: list[str], start: int, end: int) -> int | None:
    """Where the Gates table's header is in lines[start:end]: the first `|` row whose first cell is `Gate`, with
    three cells or more and a rule under it. A legend before the table, headed `Gate` or not, is not it."""
    for i in range(start, end - 1):
        cells = clocks.split_cells(lines[i]) if lines[i].startswith("|") else []
        if len(cells) >= 3 and cells[0].lower() == "gate" and SEPARATOR.match(lines[i + 1]):
            return i
    return None


def _ledger_header(lines: list[str], name: str) -> list[str]:
    """The header and rule a new archive's table copies from the ledger. For Gates, the Gates table's
    (`_gates_header`); for Decisions, the section's first table's."""
    span = _section(lines, name)
    if span is None:
        return []
    if name == "Decisions":
        return _header(lines, _table_block(lines, *span))
    at = _gates_header(lines, *span)
    return [] if at is None else lines[at : at + 2]


def _section_rows(lines: list[str], name: str) -> list[str]:
    """Every row of the section's tables that the archive counts: numbered Decisions rows, and gate rows."""
    span = _section(lines, name)
    if span is None:
        return []
    start, end = span
    if name == "Decisions":
        return [lines[i] for i in range(start, end) if DECISION_ROW.match(lines[i])]
    return [lines[start + i] for rows in clocks.gate_table(lines[start:end]).values() for i, _ in rows]


def _continued(lines: list[str], i: int, end: int) -> bool:
    """Whether the next line carries on row `i`: text that is no blank line, heading or comment, or a table row
    whose first cell is empty (`| | | answered 11-03 |`, written under the row it adds to). A row broken over two
    lines would leave its second half behind if it moved, so it stays. Its second half may start with `#` ("#41
    for the fix"): only `#` and a space start a heading. A row with a first cell of its own (`| — |`, `| 5a |`) is
    a row of its own, and the archive's own pointer line is no second half, wherever the manager puts it."""
    j = i + 1
    if j >= end:
        return False
    line = lines[j]
    if line.startswith("|"):
        return clocks.plain_cells(line)[0] == ""
    return line.strip() != "" and not HEADING.match(line) and not line.lstrip().startswith("<!--") and not line.startswith(POINTER)


def _moving(lines: list[str], keep: int = KEEP_DECISIONS, swept: set[str] | None = None) -> dict:
    """The line indices that move out of the ledger, by section, and what they are. With `swept` (the lines of a
    sweep file), only what that sweep listed: a row added or changed since stays, counted under `unswept`. A row
    the next line carries on stays too, named under `broken`, to be mended in the ledger."""
    out = {
        "Decisions": [], "Gates": [], "numbers": [], "gates": [],
        "unswept": {"decisions": 0, "gates": 0}, "broken": {"decisions": [], "gates": [], "unclear_gates": []},
    }
    span = _section(lines, "Decisions")
    if span:
        rows = [(i, int(m.group(1))) for i in range(*span) for m in [DECISION_ROW.match(lines[i])] if m]
        kept = set(sorted({n for _, n in rows}, reverse=True)[:keep])
        due = [(i, n) for i, n in rows if n not in kept]
        whole = [(i, n) for i, n in due if not _continued(lines, i, span[1])]
        out["broken"]["decisions"] = sorted({n for i, n in due} - {n for _, n in whole})
        going = [(i, n) for i, n in whole if swept is None or lines[i] in swept]
        out["Decisions"] = [i for i, _ in going]
        out["numbers"] = sorted({n for _, n in going})
        out["unswept"]["decisions"] = len(whole) - len(going)
    span = _section(lines, "Gates")
    if span:
        start, end = span
        table = clocks.gate_table(lines[start:end])
        for gate, rows in table.items():
            if clocks.gate_unclear(rows):
                out["broken"]["unclear_gates"].append(gate)
                continue
            if not clocks.gate_settled(rows):
                continue
            if any(_continued(lines, start + i, end) for i, _ in rows):
                out["broken"]["gates"].append(gate)
                continue
            if swept is not None and any(lines[start + i] not in swept for i, _ in rows):
                out["unswept"]["gates"] += 1
                continue
            out["gates"].append(gate)
            out["Gates"] += [start + i for i, _ in rows]
        out["Gates"].sort()
    return out


def ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 5] as "1-3, 5". A number written twice (a row the ledger holds twice) is named once."""
    out, run = [], []
    for n in sorted(set(numbers)):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [n]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


# ---------------------------------------------------------------- follow-ups


def report_digests(rows: list[dict]) -> dict[str, str]:
    """Each report's follow-ups as a digest. A report is moved, and kept out of FOLLOW-UPS.md, only while it reads
    as it did when it was swept: a verdict written again (a review given back and done again under the same number,
    a run voided and made again under the same id) is listed again, and read again before it moves."""
    by_report: dict[str, list[list[str]]] = {}
    for row in rows:
        by_report.setdefault(row["report"], []).append([row["kind"], row["severity"], row["what"]])
    return {report: hashlib.sha256(json.dumps(found).encode()).hexdigest()[:16] for report, found in by_report.items()}


def archived_reports(task) -> dict[str, str]:
    """The reports whose follow-ups moved to FOLLOW-UPS-ARCHIVE.md (paths relative to the loop root), each with
    the digest it had when it moved. Unreadable, it raises: a caller that writes FOLLOW-UPS.md lists every row
    then, never fewer."""
    path = Path(task) / ARCHIVED_REPORTS
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    reports = data.get("reports") if isinstance(data, dict) else None
    if not isinstance(reports, dict) or not all(isinstance(v, str) for v in reports.values()):
        raise ArchiveError(f"{path.name} holds no reports with their digests")
    return {str(k): v for k, v in reports.items()}


def gone_reports(rows: list[dict], archived: dict[str, str]) -> set[str]:
    """The archived reports that still read as they did when they moved: their rows stay out of FOLLOW-UPS.md."""
    current = report_digests(rows)
    return {report for report, digest in archived.items() if current.get(report) == digest}


def _followup_bytes(rows: list[dict]) -> int:
    return len(ledger._table(["Kind", "Severity", "What", "From"], ledger.followup_table_rows(rows))) if rows else 0


# ---------------------------------------------------------------- writing


class _Line(str):
    """A line as read from a file, with the line break that ended it."""

    end = ""


def _split(data: bytes) -> tuple[list[str], str]:
    """A file's lines, and the newline for the lines the archive adds to it.

    Lines are split where Markdown ends a line (`clocks.LINE_END`: "\\r\\n", a lone "\\r", "\\n"), exactly as the
    clocks fact splits the ledger (`clocks.split_lines`): were the two to split it differently, a gate's open row
    could be read by one and not by the other, and the gate moved. Each line keeps the break it ended with, so a
    file is written back as it was but for the lines that move or are added. Like `str.split("\\n")`, a file that
    ends with a break has one more, empty, line. The newline is "\\r\\n" when every "\\n" in the file ends one,
    else "\\n"."""
    text = data.decode("utf-8")
    lines: list[str] = []
    start = 0
    for found in clocks.LINE_END.finditer(text):
        line = _Line(text[start : found.start()])
        line.end = found.group()
        lines.append(line)
        start = found.end()
    lines.append(_Line(text[start:]))
    crlf = text.count("\r\n")
    return lines, "\r\n" if crlf and crlf == text.count("\n") else "\n"


def _join(lines: list[str], newline: str) -> str:
    """The lines as a file: each followed by the break it was read with. A line the archive made, or one read last
    with no break that no longer is last, takes `newline`; the last line takes no break it was not read with. A
    line read with a break keeps it when the line after it moves, the file's last line too."""
    out = []
    for k, line in enumerate(lines):
        out.append(line)
        end = getattr(line, "end", "")
        out.append(end if k == len(lines) - 1 else end or newline)
    return "".join(out)


def _kept_break(new: str, old: str) -> str:
    """`new` in the place of `old`, ending with the break `old` was read with."""
    line = _Line(new)
    line.end = getattr(old, "end", "")
    return line


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()  # never left for the next save to commit
        except OSError:
            pass
        raise


def _write_all(writes: list[tuple[Path, str]], ledger_path: Path, ledger_read: bytes) -> None:
    """Write each file in turn, reading each back, byte for byte, as soon as it is written.

    `writes` puts each file that gains rows before the file that loses them: the ledger's archive before the
    ledger, FOLLOW-UPS-ARCHIVE.md before the record and FOLLOW-UPS.md. So whatever stops it part way, every row is
    in one file or in both, never in neither. The ledger is not written over when it no longer reads as it did when
    the move was worked out from it (a save, or a sub-agent's edit, at the same moment): what that writer wrote
    would be lost.

    On a failure the files written are put back, the last written first, and the failure raised. A file that
    cannot be put back, or that no longer reads as it was written (another writer has written it since, and
    putting it back would lose that), is left as it is, and so is every file written before it: putting back the
    archive under a ledger that still lacks its rows would leave those rows in neither. UndoError names what was
    left."""
    data = [(path, text.encode("utf-8")) for path, text in writes]
    before = {path: (path.read_bytes() if path.exists() else None) for path, _ in data}
    written: list[tuple[Path, bytes]] = []
    try:
        for path, new in data:
            if path == ledger_path and path.read_bytes() != ledger_read:
                raise ArchiveError("the ledger changed while the archive was being worked out; run the archive again")
            _atomic_write(path, new)
            written.append((path, new))
            # Read back at once: a save that writes the ledger a moment later is no failure of this one.
            if path.read_bytes() != new:
                raise ArchiveError(f"{path.name} did not read back as written")
    except Exception as exc:
        for k in range(len(written) - 1, -1, -1):
            path, new = written[k]
            try:
                if path.read_bytes() != new:
                    raise ArchiveError("it was written again since, by another writer")
                if before[path] is None:
                    path.unlink()
                else:
                    _atomic_write(path, before[path])
            except (OSError, ArchiveError) as undo:
                left = [p.name for p, _ in written[: k + 1]]
                raise UndoError(
                    f"{exc}; {path.name} could not be put back ({undo}), so it and the files written before it are left as they are: {', '.join(left)}"
                ) from exc
        raise


def _bare(lines: list[str]) -> list[str]:
    """The lines without the archive's pointer lines and the blank line that follows each."""
    out, after_pointer = [], False
    for line in lines:
        if line.startswith(POINTER):
            after_pointer = True
            continue
        if after_pointer and line == "":
            after_pointer = False
            continue
        after_pointer = False
        out.append(line)
    return out


def _pointer(name: str, archive_lines: list[str], at: str) -> str:
    if name == "Decisions":
        numbers = [int(DECISION_ROW.match(r).group(1)) for r in _section_rows(archive_lines, "Decisions")]
        held = f"Decisions {ranges(numbers)}"
        find = "`grep -n '^| <n> |' LEDGER-ARCHIVE.md`"
    else:
        span = _section(archive_lines, "Gates")
        gates = clocks.gate_table(archive_lines[span[0] : span[1]]) if span else {}
        held = f"{len(gates)} settled gates"
        find = "`grep -n '<gate>' LEDGER-ARCHIVE.md`"
    return f"{POINTER} holds {held}, moved by `hl-flow archive` (last at {at}). Find one with {find}."


def _new_ledger(lines: list[str], moving: dict, archive_lines: list[str], at: str) -> list[str]:
    """The ledger without the rows that move, with one pointer line per section that sent any (or had sent any
    before: its pointer is kept up to date)."""
    gone = set(moving["Decisions"]) | set(moving["Gates"])
    out = [line for i, line in enumerate(lines) if i not in gone]
    for name in SECTIONS:
        span = _section(out, name)
        if span is None:
            continue
        start, end = span
        had = next((i for i in range(start, end) if out[i].startswith(POINTER)), None)
        if had is None and not moving[name]:
            continue
        line = _pointer(name, archive_lines, at)
        if had is not None:
            out[had] = _kept_break(line, out[had])
            continue
        block = _table_block(out, start, end)
        at_line = block[0] if block else end
        header = _gates_header(out, start, end) if name == "Gates" else None
        if header is not None:  # above the Gates table, not a legend before it; above any row run into its header
            at_line = header
            while at_line > start and out[at_line - 1].startswith("|"):
                at_line -= 1
        out[at_line:at_line] = [line, ""]
    return out


def _new_archive(existing: list[str] | None, moved: dict[str, list[str]], ledger_lines: list[str], program: str) -> list[str]:
    """The archive with the moved rows added: Decisions merged in number order, gates after the ones it holds."""
    if existing is None:
        existing = [
            f"# Ledger archive: {program}",
            "",
            f"Rows `hl-flow archive` moved out of `LEDGER.md`: every Decision but the newest {KEEP_DECISIONS}, and every gate whose last "
            "row settled it. A sub-agent read them for defects to place before they moved. History: find a row with `grep -n`, never "
            "read the file whole. The ledger's own rows are the live ones; nothing here is edited by hand.",
        ]
    lines = list(existing)
    for name in SECTIONS:
        if _section(lines, name) is None:
            lines += ["", f"## {name}", "", *(_ledger_header(ledger_lines, name) or DEFAULT_HEADERS[name])]
        start, end = _section(lines, name)
        block = _table_block(lines, start, end)
        if block is None:
            lines[end:end] = ["", *DEFAULT_HEADERS[name]]
            start, end = _section(lines, name)
            block = _table_block(lines, start, end)
        first, last = block
        header = _header(lines, block)
        rows = lines[first + len(header) : last] + moved[name]
        if name == "Decisions":
            rows.sort(key=lambda r: int(DECISION_ROW.match(r).group(1)) if DECISION_ROW.match(r) else float("inf"))
        lines[first:last] = header + rows
    if lines and lines[-1] != "":
        lines.append("")
    return lines


def _counted(ledger_lines: list[str], archive_lines: list[str] | None) -> Counter:
    rows: Counter = Counter()
    for lines in (ledger_lines, archive_lines or []):
        for name in SECTIONS:
            rows.update(_section_rows(lines, name))
    return rows


# ---------------------------------------------------------------- the verbs


def plan(root, ledger_file, rows: list[dict] | None = None, swept: dict | None = None, data: bytes | None = None) -> dict:
    """What `archive` would move now, and its size. Writes nothing. With `swept` (`read_swept`), only what that
    sweep listed, and how much more would move were it not for that (`unswept`). `data` is the ledger as already
    read: the move is worked out from the one read it writes against."""
    ledger_path = Path(ledger_file)
    data = ledger_path.read_bytes() if data is None else data
    lines, newline = _split(data)
    moving = _moving(lines, swept=None if swept is None else swept["lines"])
    task = ledger_path.parent
    rows = ledger.followups(root) if rows is None else rows
    archived = archived_reports(task)
    current = report_digests(rows)
    gone = {report for report, digest in archived.items() if current.get(report) == digest}
    due = [r for r in rows if r["report"] not in gone and not r["live"]]
    leaving = [r for r in due if swept is None or swept["reports"].get(r["report"]) == current[r["report"]]]
    decision_lines = [str(lines[i]) for i in moving["Decisions"]]
    gate_lines = [str(lines[i]) for i in moving["Gates"]]
    moved_bytes = sum(len(line) + 1 for line in decision_lines + gate_lines) + _followup_bytes(leaving)
    planned = {
        "ledger_kb": round(len(data) / 1024),
        "movable_kb": round(moved_bytes / 1024),
        "decisions": {"rows": len(decision_lines), "numbers": ranges(moving["numbers"]), "kept": KEEP_DECISIONS},
        "gates": {"rows": len(gate_lines), "gates": moving["gates"]},
        "followups": {"reports": len({r["report"] for r in leaving}), "rows": len(leaving)},
        "_moving": moving,
        "_ledger": (lines, newline),
        "_lines": {"Decisions": decision_lines, "Gates": gate_lines},
        "_leaving": leaving,
        "_digests": {r["report"]: current[r["report"]] for r in leaving},
        "_archived": archived,
    }
    if swept is not None:
        planned["unswept"] = {**moving["unswept"], "followup_rows": len(due) - len(leaving)}
    if any(moving["broken"].values()):
        planned["broken"] = moving["broken"]
    return planned


SWEEP_TITLE = "# About to be archived: "
SWEEP_REPORTS = "## Reports these follow-ups came from"
SWEEP_REPORT = re.compile(r"^- `([^`]+)` ([0-9a-f]{16})$")


def sweep_text(planned: dict, program: str) -> str:
    """What moves, in one file, for the sub-agent that reads it for defects to place before it goes. The real
    archive is given this file back (`--swept`) and moves only what it lists."""
    out = [
        f"{SWEEP_TITLE}{program}",
        "",
        "Every row below leaves `LEDGER.md` and `FOLLOW-UPS.md` at the next `hl-flow archive --swept <this file>`, and nothing "
        "else does. Read it for defects recorded as noted, to place or later that no item has placed, and for reported defects "
        "with a clear fix that no item acted on.",
        "",
        "## Gates",
        "",
        *planned["_lines"]["Gates"],
        "",
        "## Decisions",
        "",
        *planned["_lines"]["Decisions"],
        "",
        "## Follow-ups",
        "",
        ledger._table(["Kind", "Severity", "What", "From"], ledger.followup_table_rows(planned["_leaving"])),
        "",
        SWEEP_REPORTS,
        "",
        *(f"- `{report}` {digest}" for report, digest in sorted(planned["_digests"].items())),
        "",
    ]
    return "\n".join(out)


def read_swept(path) -> dict:
    """The rows a sweep file lists, and its reports with the digest each had. Refused (ArchiveError) for a file
    `sweep_text` did not write."""
    lines = [str(line) for line in _split(Path(path).read_bytes())[0]]
    if not lines or not lines[0].startswith(SWEEP_TITLE):
        raise ArchiveError(f"{path} is not a file `hl-flow archive --dry-run --out` wrote")
    start = next((i for i, line in enumerate(lines) if line == SWEEP_REPORTS), len(lines))
    reports = {m.group(1): m.group(2) for line in lines[start:] for m in [SWEEP_REPORT.match(line)] if m}
    return {"lines": set(lines[:start]), "reports": reports}


def public(planned: dict) -> dict:
    return {k: v for k, v in planned.items() if not k.startswith("_")}


def fact(root, ledger_file, at_kb, rows: list[dict] | None = None) -> dict:
    """The `archive` fact of a save: the ledger's size, what could move, and whether that has reached `at_kb`."""
    planned = plan(root, ledger_file, rows)
    return {"ledger_kb": planned["ledger_kb"], "movable_kb": planned["movable_kb"], "at_kb": at_kb, "due": planned["movable_kb"] >= at_kb}


def apply(root, ledger_file, program: str, at: str, swept: dict, rows: list[dict] | None = None) -> dict:
    """Move what `plan` lists and the sweep `swept` (`read_swept`) read. Answers what moved, or `nothing-to-move`;
    either way `unswept` says what would have moved but changed or arrived after the sweep, and stays for the next.

    The ledger is read once, and the move worked out from that read. Before anything is written: the ledger must
    come out as it was but for the rows that move and the pointer lines, and the rows of the ledger and the
    archive together must be the same rows. Then every file is written or none is (`_write_all`).

    FOLLOW-UPS-ARCHIVE.md is only added to: each archive adds a section of what it moved and leaves the rest as it
    was. Its rows are not written again from the reports, which may be gone by then (a run voided by hand is no
    run, and its reports are read no more); the record likewise keeps every report it ever took."""
    ledger_path = Path(ledger_file)
    task = ledger_path.parent
    archive_path = task / ARCHIVE
    rows = ledger.followups(root) if rows is None else rows
    before_bytes = ledger_path.read_bytes()
    planned = plan(root, ledger_file, rows, swept, before_bytes)
    if not (planned["decisions"]["rows"] or planned["gates"]["rows"] or planned["followups"]["rows"]):
        return {"result": "nothing-to-move", **public(planned)}
    lines, newline = planned["_ledger"]
    moving = planned["_moving"]
    writes: list[tuple[Path, str]] = []
    new_ledger_text = before_bytes.decode("utf-8")
    if moving["Decisions"] or moving["Gates"]:
        old_archive, archive_newline = _split(archive_path.read_bytes()) if archive_path.exists() else (None, newline)
        # A row that moves takes the archive's line break, as every line the archive adds does.
        moved = {name: [str(lines[i]) for i in moving[name]] for name in SECTIONS}
        if old_archive and old_archive[-1] == "":
            old_archive = old_archive[:-1]
        new_archive = _new_archive(old_archive, moved, [str(line) for line in lines], program)
        new_ledger = _new_ledger(lines, moving, new_archive, at)
        leaving = set(moving["Decisions"]) | set(moving["Gates"])
        if _bare(new_ledger) != _bare([line for i, line in enumerate(lines) if i not in leaving]):
            raise ArchiveError("the ledger would change beyond the rows that move; nothing was written")
        if _counted(lines, old_archive) != _counted(new_ledger, new_archive):
            raise ArchiveError("the rows of the ledger and its archive would differ after the move; nothing was written")
        new_ledger_text = _join(new_ledger, newline)
        writes += [(archive_path, _join(new_archive, archive_newline)), (ledger_path, new_ledger_text)]
    if planned["_leaving"]:
        followups_archive = task / FOLLOWUPS_ARCHIVE
        existing, followups_newline = None, "\n"
        if followups_archive.exists():
            existing_bytes = followups_archive.read_bytes()
            existing, followups_newline = existing_bytes.decode("utf-8"), _split(existing_bytes)[1]
        archived = {**planned["_archived"], **planned["_digests"]}
        gone = gone_reports(rows, archived)
        writes += [
            (followups_archive, ledger.followups_archive_text(existing, program, at, planned["_leaving"], followups_newline)),
            (task / ARCHIVED_REPORTS, json.dumps({"archived_at": at, "reports": dict(sorted(archived.items()))}, indent=1) + "\n"),
            (task / FOLLOWUPS, ledger.followups_text(root, program, at, [r for r in rows if r["report"] not in gone])),
        ]
    _write_all(writes, ledger_path, before_bytes)
    return {"result": "archived", **public(planned), "ledger_kb_after": round(len(new_ledger_text.encode()) / 1024)}
