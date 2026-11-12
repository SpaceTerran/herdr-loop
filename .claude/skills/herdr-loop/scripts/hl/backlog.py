"""Mechanical parser for backlog ITEMS files.

Ported from LoopRunner's `looprunner/backlog.py` (same markers, same Verify
block, same numbered-list rules) with two deliberate differences: a heading
may use an em dash, an en dash or a hyphen after the id, and one file may be
read alongside others, so ids are checked for duplicates across the whole set
rather than limited to one prefix per file. No model is involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^##\s+([A-Za-z][A-Za-z0-9]*-\d+)\s+[—–-]\s+(.+?)\s*$")
NUMBERED_ITEM_RE = re.compile(r"^(\d+)\.\s+(.+)$")
ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")
MARKER_RE = re.compile(r"^\*\*[^*]+\.\*\*")

MARKER_SCOPE = "**Scope.**"
MARKER_ACCEPT = "**Accept.**"
MARKER_DEPENDS = "**Depends.**"
MARKER_NOT = "**Not.**"
MARKER_VERIFY = "**Verify.**"
MARKER_FLOOR = "**Floor.**"

# What an item's author may require of how the item is routed, whatever the
# manager would choose by itself: an independent premise check before the
# build, a look in a real browser at the review, a Sonnet builder from the
# first build. The manager may go above a floor and never below it.
FLOORS = ("premise", "look", "sonnet")

VERIFY_COMMAND_RE = re.compile(r"^\*\*Verify\.\*\*\s+`([^`\n]+)`\s*$")
VERIFY_WAIT_RE = re.compile(r"^Wait\.\s*(\S+)\s*$")
VERIFY_WAIT_DEFAULT_MIN = 15
VERIFY_WAIT_CEILING_MIN = 60


class BacklogError(ValueError):
    """The file does not parse. The message names the item."""


@dataclass
class Item:
    id: str
    title: str
    text: str  # the section verbatim, heading included
    accept: list[str]
    depends_on: list[str] = field(default_factory=list)
    verify_command: str | None = None
    verify_wait_min: int | None = None
    floor: list[str] = field(default_factory=list)


def parse_backlog(text: str) -> list[Item]:
    """Every `## <ID> — title` section, in file order.

    Raises BacklogError when a section has no Scope paragraph or no Accept
    list, when an id repeats, or when a Verify block is malformed. A broken
    backlog fails here, loudly, rather than producing a short plan.
    """
    lines = text.splitlines()
    headings: list[tuple[int, str, str]] = []
    seen: dict[str, str] = {}
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m:
            continue
        item_id, title = m.group(1), m.group(2)
        if item_id in seen:
            raise BacklogError(f'duplicate id {item_id}: "{seen[item_id]}" and "{title}"')
        seen[item_id] = title
        headings.append((i, item_id, title))

    items: list[Item] = []
    for idx, (start, item_id, title) in enumerate(headings):
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        section = lines[start:end]
        while section and section[-1].strip() in ("", "---"):
            section.pop()
        blocks = _split_blocks(lines[start + 1 : end])

        if not any(_joined(b).startswith(MARKER_SCOPE) for b in blocks):
            raise BacklogError(f"{item_id}: no {MARKER_SCOPE} paragraph found")
        accept = _numbered_list(blocks, MARKER_ACCEPT)
        if not accept:
            raise BacklogError(f"{item_id}: no {MARKER_ACCEPT} list found")
        verify_command, verify_wait_min = _verify_block(blocks, item_id)

        items.append(
            Item(
                id=item_id,
                title=title,
                text="\n".join(section) + "\n",
                accept=accept,
                depends_on=_id_list(blocks, MARKER_DEPENDS),
                verify_command=verify_command,
                verify_wait_min=verify_wait_min,
                floor=_floor(blocks, item_id),
            )
        )
    return items


# What an Accept line should not ask for. Suites and required checks are held green at the merge by a script, on
# the commit that merges; written into a criterion they are run again by every builder and reviewer, and a
# criterion about a test runner's own output fails for the runner's settings, not for the build.
_LINT = (
    (re.compile(r"gh pr checks|required checks?", re.IGNORECASE), "asks for the required checks: the merge holds them green"),
    (re.compile(r"--watch\b"), "waits on checks with --watch: no worker waits on CI"),
    (re.compile(r"\bnpm (ci|run (test|build|typecheck|lint))\b|\b(pytest|vitest|jest|go test|cargo test)\b(?![^`]*(::|/[\w.-]+\.(py|[jt]sx?|go|rs)))"), "runs a whole suite: name the behaviour, or the one test that shows it"),
    (re.compile(r"\d+ passed|<N> passed|\bpassed\b.*\b(last|summary) line|\b(last|summary) line\b.*\bpassed\b", re.IGNORECASE), "expects a line of a test runner's summary: that depends on the runner's settings"),
)


def lint(item: Item) -> list[str]:
    """Remarks on an item's Accept list for whoever wrote it. Never a refusal:
    an older backlog runs as it is, and its author decides."""
    out = []
    for n, line in enumerate(item.accept, start=1):
        for pattern, remark in _LINT:
            if pattern.search(line):
                out.append(f"{item.id} criterion {n} {remark}")
    return out


def _split_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _joined(block: list[str]) -> str:
    return " ".join(line.strip() for line in block)


def _id_list(blocks: list[list[str]], marker: str) -> list[str]:
    for block in blocks:
        joined = _joined(block)
        if joined.startswith(marker):
            return ID_RE.findall(joined[len(marker) :])
    return []


def _numbered_list(blocks: list[list[str]], marker: str) -> list[str]:
    for i, block in enumerate(blocks):
        first = block[0].strip()
        if not first.startswith(marker):
            continue
        remainder = first[len(marker) :].strip()
        rest = ([remainder] if remainder else []) + block[1:]
        # A blank line ends the list only before a paragraph that is not part of it: an indented
        # paragraph continues the entry above it, and a numbered line is the next entry.
        for following in blocks[i + 1 :]:
            if not (following[0][:1].isspace() or NUMBERED_ITEM_RE.match(following[0])):
                break
            rest += following
        return _parse_numbered_lines(rest)
    return []


def numbered_lines(text: str, marker: str) -> int:
    """How many lines of the section under `marker` start a numbered entry,
    counted down to the next `**Marker.**` paragraph or heading without regard
    to blank lines, and not inside a fenced code block: what `_numbered_list`
    should have found."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(marker)), None)
    if start is None:
        return 0
    count, fenced = 0, False
    for line in [lines[start][len(marker) :].strip()] + lines[start + 1 :]:
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if MARKER_RE.match(line) or line.startswith("#"):
            break
        count += bool(NUMBERED_ITEM_RE.match(line))
    return count


def _parse_numbered_lines(lines: list[str]) -> list[str]:
    """`1. text` at column 0 starts an entry; any other line continues it,
    an indented numbered sub-list included."""
    out: list[str] = []
    current: list[str] | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = NUMBERED_ITEM_RE.match(raw)
        if m:
            if current is not None:
                out.append(" ".join(current))
            current = [m.group(2).strip()]
        elif current is not None:
            current.append(line)
    if current is not None:
        out.append(" ".join(current))
    return out


def _floor(blocks: list[list[str]], item_id: str) -> list[str]:
    """`**Floor.** premise, look: why` gives the floors before the colon; the
    reason after it is for people. A word that is not a floor is refused: a
    floor that was misspelled would otherwise protect nothing."""
    for block in blocks:
        joined = _joined(block)
        if not joined.startswith(MARKER_FLOOR):
            continue
        names = joined[len(MARKER_FLOOR) :].partition(":")[0]
        words = [w.strip().rstrip(".").strip().lower() for w in names.split(",")]
        words = [w for w in words if w]
        unknown = [w for w in words if w not in FLOORS]
        if unknown or not words:
            raise BacklogError(
                f"{item_id}: malformed {MARKER_FLOOR} line: expected one or more of {', '.join(FLOORS)}, "
                f"separated by commas, then optionally ': why'; got {joined[len(MARKER_FLOOR) :].strip()!r}"
            )
        return [f for f in FLOORS if f in words]
    return []


def without_verify(text: str) -> str:
    """An item's paragraphs without its Verify block (the `**Verify.**` line
    and the `Wait.` lines under it), one blank line between each. What is
    left is everything an item's contract and briefs are made from, so two
    texts that are equal here differ only in their deploy check."""
    blocks = [b for b in _split_blocks(text.splitlines()) if not b[0].strip().startswith(MARKER_VERIFY)]
    return "\n\n".join("\n".join(b) for b in blocks)


def _verify_block(blocks: list[list[str]], item_id: str) -> tuple[str | None, int | None]:
    for block in blocks:
        first = block[0].strip()
        if not first.startswith(MARKER_VERIFY):
            continue
        m = VERIFY_COMMAND_RE.match(first)
        if not m:
            raise BacklogError(
                f"{item_id}: malformed {MARKER_VERIFY} block: expected "
                f"'{MARKER_VERIFY} `<command>`' on one line, got {first!r}"
            )
        wait_min = VERIFY_WAIT_DEFAULT_MIN
        for raw in block[1:]:
            line = raw.strip()
            if not line:
                continue
            wm = VERIFY_WAIT_RE.match(line)
            if not wm or not wm.group(1).isdigit():
                raise BacklogError(
                    f"{item_id}: malformed {MARKER_VERIFY} block: expected 'Wait. <minutes>', got {line!r}"
                )
            wait_min = int(wm.group(1))
            if not 1 <= wait_min <= VERIFY_WAIT_CEILING_MIN:
                raise BacklogError(
                    f"{item_id}: {MARKER_VERIFY} Wait. {wait_min} is out of range 1-{VERIFY_WAIT_CEILING_MIN}"
                )
        return m.group(1), wait_min
    return None, None
