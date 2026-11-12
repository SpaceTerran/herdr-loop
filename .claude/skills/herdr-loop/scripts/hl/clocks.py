"""Where a program's text waits on a clock, a reading, or another run closing.

The operator's rule (2026-11-10): nobody waits for a queue, a clock or a
reading to see whether something fixes itself. A wait names what it would
catch, and that reading is taken now; only a protection for people (a user
who could be locked out, data that cannot be restored) keeps a clock. An
order between runs holds only for an output the other run makes.

On the help-desk catalog program that day every stream stood idle for about
four hours: an item was to be built only if a Renovate queue had not drained
in a day, and a run header ("after run `hd-lambda-lock` closes") held three
items that needed nothing from that run. An account security sweep
(2026-10-27) and a dependency pin (2026-11-02) had waited the same way.

Three kinds of words are found:

    wait   says a wait outright: a next read, a soak, T+, after tonight, a
           drain reading, "if the queue does not drain", until a date
    clock  a length of time beside a word of waiting ("after 24 h", "7 days
           after"). In an item's text it is far more often the product's own
           timing (a reminder 3 days after a request) than a wait, so it is
           looked for only where a wait is the likelier reading: a spec's
           prose and a run's header at staging, and the ledger's Gates
           rows at every save
    edge   an order on another run's end ("after run `hd-lambda-lock`
           closes", "when `app-backend` is complete")

This finds them, for staging's remarks (`clock_remarks`) and for the
`clocks` fact in `hl-flow save`'s answer. Which wait protects a person, and
what reading replaces the rest, is the manager's judgement and the
author's: nothing here refuses or decides.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import backlog, items, ledger

WAIT, CLOCK, EDGE = "wait", "clock", "edge"
_COUNT = r"(?:a|an|one|two|three|another|\d+)"
_UNIT = r"(?:h|hrs?|hours?|days?|nights?|weeks?)"
_NOT = r"(?:(?:does|did|has|have)\s*n[o']t\s+)?"
_CLOSES = r"(?:closes|is closed|has closed|completes|is complete|has completed|ends|has ended|is done|finishes|has finished)"
PATTERNS = {
    WAIT: (
        re.compile(r"\bnext read(?:ing)?\b", re.I),
        re.compile(r"\bT\s*\+\s*\d", re.I),
        re.compile(r"\bsoak(?:s|ed|ing)?\b", re.I),
        re.compile(r"\bafter tonight\b", re.I),
        re.compile(r"\b(?:drain|queue|backlog)\s+reading\b", re.I),
        re.compile(rf"\bdrain(?:s|ed)?\s+(?:in|within)\s+{_COUNT}\s*-?\s*{_UNIT}\b", re.I),
        # "If the Renovate queue does not drain", "whether the queue then drains". "Drain" alone is a product's word
        # as often as a wait's (a help-desk product's search-index drain), so only a queue or backlog left to clear
        # counts.
        re.compile(rf"\b(?:if|whether|unless|until|once)\s+(?:the\s+)?(?:[\w-]+\s+)?(?:queue|backlog)\s+(?:then\s+)?{_NOT}(?:drain(?:s|ed)?|clear(?:s|ed)?|empt(?:y|ies|ied))\b", re.I),
        re.compile(r"\buntil\s+~?(?:\d{4}-)?\d{1,2}-\d{1,2}(?!\d)", re.I),
    ),
    CLOCK: (
        re.compile(rf"\b(?:after|again in|wait(?:s|ed|ing)?(?: for)?|hold(?:s|ing)?(?: for)?|held(?: for)?|observe(?: for)?|watch(?: for)?)\s+(?:the next\s+)?{_COUNT}\s*-?\s*{_UNIT}\b", re.I),
        re.compile(rf"\b{_COUNT}\s*-?\s*{_UNIT}\s+(?:later|after|from now)\b", re.I),
        re.compile(r"\bovernight\b|\btomorrow\b|\bnext (?:scan|morning|night)\b", re.I),
    ),
    # A backticked name in lower case is a run id; an item's id (`HD-63`) is a dependency, which the scripts hold.
    EDGE: (
        re.compile(rf"\b(?:after|once|when|until)\s+(?:the\s+)?run\s+`?[A-Za-z0-9][\w.-]*`?\s+{_CLOSES}\b", re.I),
        re.compile(rf"\b(?:after|once|when|until)\s+`[a-z][a-z0-9.-]*`\s+{_CLOSES}\b"),
    ),
}
ALL = (WAIT, CLOCK, EDGE)
IN_ITEMS = (WAIT, EDGE)
REMARK = {
    WAIT: "waits to see what happens: name what the wait would catch and read it now; only a protection for people (a lock-out, data that cannot be restored) keeps a wait",
    CLOCK: "names a length of time: if it is a wait and not the product's own timing, name what it would catch and read it now; only a protection for people keeps a clock",
    EDGE: "orders on another run closing: name the output it needs from that run, or it is not an edge",
}
# A Gates row whose result says the gate is settled. A gate is read by its last row: a table of readings keeps the
# earlier ones ("not passed", next read …) below the row that passed it.
SETTLED = re.compile(r"^(?:passed|done|closed|dropped|met|deleted|removed|skipped|superseded|cannot hold|n/a)\b", re.I)
GATE_ID = re.compile(r"^[*`_ ]*([A-Za-z][\w-]*)")
SAYS_CHARS = 90


def _says(text: str, start: int, end: int) -> str:
    """The match with a little of its line around it, on one line."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    # Centred on the match, and what one side of the line cannot use goes to the other.
    lo = max(line_start, start - max(0, SAYS_CHARS - (end - start)) // 2)
    hi = min(line_end, max(end, lo + SAYS_CHARS))
    lo = max(line_start, min(lo, hi - SAYS_CHARS))
    said = " ".join(text[lo:hi].split())
    return ("…" if lo > line_start else "") + said + ("…" if hi < line_end else "")


def found(text: str, kinds=ALL) -> list[tuple[str, str]]:
    """Each wait, clock and run edge in `text`, as (kind, what it says), in the order they come. Words two patterns
    both match are one place, said once, by the pattern that starts first."""
    spans = sorted((m.start(), m.end(), kind) for kind in kinds for pattern in PATTERNS[kind] for m in pattern.finditer(text))
    out, reached = [], -1
    for start, end, kind in spans:
        if start < reached:
            reached = max(reached, end)
            continue
        out.append((kind, _says(text, start, end)))
        reached = end
    return out


def header(text: str) -> str:
    """A backlog file's text above its first item: what the run says of itself, which reaches no worker."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if backlog.HEADING_RE.match(line):
            return "\n".join(lines[:i])
    return text


def in_item(text: str) -> list[tuple[str, str]]:
    """An item's waits and run edges. Its Verify `Wait.` is a deadline from the merge, not a soak, and is left out."""
    return found(backlog.without_verify(text), IN_ITEMS)


def backlog_hits(text: str, name: str) -> list[tuple[str, str, str]]:
    """(where, kind, what it says) for a backlog file: its header, then each item by id."""
    out = [(f"{name} header", kind, says) for kind, says in found(header(text))]
    try:
        parsed = backlog.parse_backlog(text)
    except backlog.BacklogError:
        parsed = []  # staging refuses it by name
    for item in parsed:
        out += [(item.id, kind, says) for kind, says in in_item(item.text)]
    return out


def remarks(where_hits) -> list[str]:
    return [f"{where} {REMARK[kind]} (\"{says}\")" for where, kind, says in where_hits]


def _ticks(text: str, at: int) -> int:
    """How many backticks run from `at`."""
    return len(text) - at - len(text[at:].lstrip("`"))


def _edges(cells: list[str], text: str) -> list[str]:
    """The cells without the empty ones the row's outer pipes make."""
    if text.startswith("|"):
        cells = cells[1:]
    if len(cells) > 1 and cells[-1] == "" and text.endswith("|"):
        cells = cells[:-1]
    return cells


def plain_cells(line: str) -> list[str]:
    """A table row's cells split at every `|` not written `\\|`, code spans or not, as GitHub splits them."""
    text = line.strip()
    return _edges([c.strip() for c in re.split(r"(?<!\\)\|", text)], text)


def split_cells(line: str) -> list[str]:
    """A table row's cells split at every `|`, written `\\|` or not, with every pipe at either end taken off: how
    the kit read a gate row before 2026-11-11, and how a person reading the raw line may."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def table_cells(line: str) -> list[str]:
    """A table row's cells, as a person reading the row takes them: a `|` inside a code span, or written `\\|`, is
    part of its cell. A gate row whose "How it was checked" quoted `grep -E 'x|done'` was otherwise read with its
    Result taken from the wrong cell. A backtick with no closing run of the same length, or written `` \\` ``, is
    only a backtick."""
    text = line.strip()
    cells: list[str] = []
    cell: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and text[i + 1 : i + 2] in ("|", "`"):
            cell.append(text[i : i + 2])
            i += 2
            continue
        if c == "`":
            n = _ticks(text, i)
            end, j = i + n, i + n  # unclosed: the run is literal
            while j < len(text):
                if text[j] != "`":
                    j += 1
                    continue
                m = _ticks(text, j)
                if m == n:
                    end = j + m
                    break
                j += m
            cell.append(text[i:end])
            i = end
            continue
        if c == "|":
            cells.append("".join(cell).strip())
            cell = []
            i += 1
            continue
        cell.append(c)
        i += 1
    cells.append("".join(cell).strip())
    return _edges(cells, text)


# A gate row's Result when whether it is settled cannot be read. No settled word starts it, so the gate stays open.
UNCLEAR = "(unclear: this row reads more than one way, or has fewer than three cells)"


def _says_settled(cells: list[str]) -> bool:
    return bool(SETTLED.match(cells[2].strip("*_` ")))


def _gate_cells(line: str) -> list[str]:
    """A gate row's cells. Read keeping code spans whole, split at every `|` not written `\\|` (as GitHub does),
    and split at every `|` (as the kit did before), a row gives the same cells unless a `|` sits inside a cell or a
    backtick is left open. The row counts as settled only when all three readings have a Result and say so, and as
    open when each reading that has a Result says open. Otherwise its Result is UNCLEAR: an open gate read as
    settled would move to the archive with its open row, unread by any script after. No reading is preferred to
    another: the header's number of cells cannot choose, since a row one cell short with a `|` in its code has
    the header's number only when it is split wrongly. An UNCLEAR row keeps all its text after the marker, so a
    wait it names is still found."""
    readings = [cells for cells in (table_cells(line), plain_cells(line), split_cells(line)) if len(cells) >= 3]
    said = {_says_settled(cells) for cells in readings}
    if said == {False} or (said == {True} and len(readings) == 3):
        return readings[0]
    cells = split_cells(line) + [""] * 2
    return cells[:2] + [UNCLEAR] + cells[2:]


def gate_table(lines: list[str]) -> dict[str, list[tuple[int, list[str]]]]:
    """The rows of the Gates section, by gate, in the order they come: (the row's index in `lines`, its cells).
    Every row the kit read before 2026-11-11 is read: a `|` line whose first cell, split at every `|`, names a gate
    and is not the header's `Gate`, wherever it sits (under a legend, after a stray `|---|` line, below a line of
    text). Leaving out a row that names a gate would let an earlier row judge the gate. A row with fewer than three
    cells is read too (its Result is UNCLEAR), unless no row of that name has three: a name the kit never read as a
    gate (a two-column legend's words) is still none."""
    gates: dict[str, list[tuple[int, list[str], bool]]] = {}
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        every = split_cells(line)
        gate = GATE_ID.match(every[0])
        if gate and every[0].lower() != "gate":
            gates.setdefault(gate.group(1), []).append((index, _gate_cells(line), len(every) >= 3))
    return {gate: [(index, cells) for index, cells, _ in rows] for gate, rows in gates.items() if any(full for _, _, full in rows)}


# Where a line of the ledger ends: CommonMark's line endings, "\r\n", a lone "\r" and "\n", as GitHub reads it.
# The other characters `str.splitlines` breaks at (U+2028, U+0085, a form feed, a vertical tab, U+001C-U+001E) are
# part of their line, as they are to GitHub, to an editor's search and to the manager.
LINE_END = re.compile(r"\r\n|\r|\n")


def split_lines(text: str) -> list[str]:
    """The text's lines, split at LINE_END. The archive splits the ledger the same way, so the two read the same
    rows. Like `str.split("\\n")`, a text that ends with a break has one more, empty, line."""
    return LINE_END.split(text)


def section(lines: list[str], name: str) -> tuple[int, int] | None:
    """(the line after `## <name>`, the line where the next section starts) for the first such section. The
    archive and the clocks fact find the Gates section, and split its lines, the same way."""
    heading = re.compile(rf"^## {re.escape(name)}\b")
    for i, line in enumerate(lines):
        if heading.match(line):
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
            return i + 1, end
    return None


def gate_result(rows: list[tuple[int, list[str]]]) -> str:
    """A gate's result: what its last row says."""
    return rows[-1][1][2].strip("*_` ")


def gate_unclear(rows: list[tuple[int, list[str]]]) -> bool:
    """Whether the gate's last row reads more than one way, or has no Result, so whether it is settled cannot be
    said."""
    return rows[-1][1][2] == UNCLEAR


def gate_settled(rows: list[tuple[int, list[str]]]) -> bool:
    return not gate_unclear(rows) and bool(SETTLED.match(gate_result(rows)))


def gate_rows(ledger_text: str) -> list[dict]:
    """The gates of the ledger's Gates table that are not settled and carry a wait, a clock or a run edge. Lines
    are split where Markdown ends a line (`split_lines`), a lone carriage return too, as the archive splits them."""
    lines = split_lines(ledger_text)
    span = section(lines, "Gates")
    if span is None:
        return []
    out = []
    for gate, rows in gate_table(lines[span[0] : span[1]]).items():
        if gate_settled(rows):
            continue
        result = gate_result(rows)
        for _, cells in rows:
            hits = found("\n".join(cells[1:]))
            if hits:
                out.append({"gate": gate, "result": " ".join(result.split())[:60], "kind": hits[0][0], "says": hits[0][1]})
                break
    return out


def program_clocks(root, ledger_file) -> dict:
    """The `clocks` fact: what is still open and waits on a reading, a clock or another run closing.

    `runs`: a run's file header (waits and run edges), while the run has an item pending, in progress or parked.
    `items`: such an item's own text, frozen in the run (waits and run edges). `gates`: a gate whose last row in the
    ledger's Gates table does not say it is settled (waits, clocks and run edges). One line each, with the first
    thing it says. A read that fails is named under `errors`, never fatal."""
    out: dict = {"runs": [], "items": [], "gates": []}
    errors = []
    for run_dir in ledger.run_dirs(root):
        try:
            run = items.load_run(run_dir)
            live = [i for i in run["order"] if items.load_item(run_dir, i)["status"] in ("pending", "in_progress", "parked")]
        except Exception as exc:  # noqa: BLE001 - a fact, never fatal
            errors.append(f"{run_dir.name}: {exc}"[:200])
            continue
        if not live:
            continue
        for record in run.get("files") or []:
            try:
                hits = found(header(Path(record["path"]).read_text()), IN_ITEMS)
            except OSError as exc:
                errors.append(f"{run['run_id']}: {exc}"[:200])
                continue
            if hits:
                out["runs"].append({"run": run["run_id"], "file": Path(record["path"]).name, "kind": hits[0][0], "says": hits[0][1]})
        for item_id in live:
            try:
                hits = in_item((items.item_dir(run_dir, item_id) / "item.md").read_text())
            except OSError as exc:
                errors.append(f"{run['run_id']} {item_id}: {exc}"[:200])
                continue
            if hits:
                out["items"].append({"run": run["run_id"], "item": item_id, "kind": hits[0][0], "says": hits[0][1]})
    if ledger_file:
        try:
            # UTF-8, as the archive reads it, whatever the locale: read as Latin-1, the 0x85 inside a ✅ is U+0085.
            out["gates"] = gate_rows(Path(ledger_file).read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"the ledger: {exc}"[:200])
    if errors:
        out["errors"] = errors
    return out
