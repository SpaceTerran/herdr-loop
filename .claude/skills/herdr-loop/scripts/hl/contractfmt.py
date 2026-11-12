"""The contract file: what "done" means for one item, as numbered criteria.

A contract is only ever written by a script, from one of three sources: the
item's own Accept list, a builder's draft an evaluator agreed to, or an
evaluator's amended text. The manager never writes a criterion. The parser is
strict on purpose: anything it cannot read one way is refused, never guessed.
"""

from __future__ import annotations

import re

CRITERIA_HEADING = "## Criteria"
OUT_OF_SCOPE_HEADING = "## Out of scope"
AMENDED_HEADING_RE = re.compile(r"^##\s+Amended contract\s*$", re.MULTILINE)
MAX_CRITERIA = 10

_TOP_LEVEL_RE = re.compile(r"^(\d+)\.\s+(\S.*)$")
_INDENTED_NUMBER_RE = re.compile(r"^\s+\d+\.\s+\S")
_HEADING_RE = re.compile(r"^##\s+\S")

SERVE_RE = re.compile(r"^\*\*Serve\.\*\*\s+`([^`\n]+)`\s*$", re.MULTILINE)
READY_RE = re.compile(r"^\*\*Ready\.\*\*\s+(URL|COMMAND)\s+`([^`\n]+)`\s*$", re.MULTILINE)


class ContractError(ValueError):
    """The contract does not parse. The message says exactly what is wrong."""


def section(text: str, heading: str) -> str:
    """The lines under `heading`, up to the next `## ` heading."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == heading:
            body: list[str] = []
            for nxt in lines[i + 1 :]:
                if _HEADING_RE.match(nxt):
                    break
                body.append(nxt)
            return "\n".join(body)
    raise ContractError(f"missing required '{heading}' section")


def parse_criteria(text: str) -> list[tuple[int, str]]:
    """The numbered criteria of `## Criteria`, as `(n, text)`.

    A criterion starts at column 0 as `N. text`; unnumbered lines continue it.
    A numbered line that is indented is refused: steps inside a criterion are
    bullets, never a nested numbered list. Numbers run 1..N with no gap or
    repeat, and N is at most 10.
    """
    body = section(text, CRITERIA_HEADING)
    criteria: list[tuple[int, str]] = []
    for raw in body.splitlines():
        if not raw.strip():
            continue
        if _INDENTED_NUMBER_RE.match(raw):
            raise ContractError(
                f"indented numbered line {raw.strip()[:60]!r}: write steps inside a criterion as bullets"
            )
        m = _TOP_LEVEL_RE.match(raw)
        if m:
            criteria.append((int(m.group(1)), m.group(2).strip()))
        elif criteria:
            n, body_text = criteria[-1]
            criteria[-1] = (n, f"{body_text} {raw.strip()}")
    if not criteria:
        raise ContractError("no numbered criteria under '## Criteria'")
    if len(criteria) > MAX_CRITERIA:
        raise ContractError(f"{len(criteria)} criteria; the limit is {MAX_CRITERIA}")
    numbers = [n for n, _ in criteria]
    if numbers != list(range(1, len(criteria) + 1)):
        raise ContractError(f"criteria must be numbered 1..{len(criteria)} in order, got {numbers}")
    return criteria


def render_from_accept(item_id: str, title: str, accept: list[str]) -> str:
    """The contract for a well-authored item: its Accept list, verbatim."""
    lines = [f"# Contract: {item_id} — {title}", "", CRITERIA_HEADING, ""]
    lines += [f"{n}. {text}" for n, text in enumerate(accept, start=1)]
    lines += [
        "",
        OUT_OF_SCOPE_HEADING,
        "",
        "Anything the item's Scope does not ask for. Sentences of the Scope that no criterion",
        "above proves are still to be built; they are not graded here.",
        "",
    ]
    return "\n".join(lines)


def extract_amended(text: str) -> str | None:
    """The complete contract an evaluator wrote under `## Amended contract`.

    Everything after that heading, minus a trailing verdict word on its own
    line. Returns None when the heading is absent.
    """
    m = AMENDED_HEADING_RE.search(text)
    if m is None:
        return None
    lines = text[m.end() :].strip("\n").splitlines()
    while lines and lines[-1].strip() == "":
        lines.pop()
    if lines and lines[-1].strip() in FINAL_WORDS:
        lines.pop()
    amended = "\n".join(lines).strip()
    return amended + "\n" if amended else None


FINAL_WORDS = ("AGREED", "REVISE", "CORRECTED", "STANDS")

DISPUTE_HEADING = "## Disputed criterion"
# Matched from the start of a line: a criterion named in the middle of a sentence is not a dispute.
_DISPUTED_RE = re.compile(r"\s*(?:[-*]\s*)?\**\s*criterion\s+(\d+)\b", re.IGNORECASE)


def disputed(text: str) -> list[int]:
    """The criteria a build report disputes: the numbers of the lines that
    begin `Criterion <n>` under `## Disputed criterion`, in order, each
    once. Empty when the report has no such section, or names no criterion
    in it."""
    try:
        body = section(text, DISPUTE_HEADING)
    except ContractError:
        return []
    found: list[int] = []
    for line in body.splitlines():
        m = _DISPUTED_RE.match(line)
        if m and int(m.group(1)) not in found:
            found.append(int(m.group(1)))
    return found


def final_word(text: str) -> str | None:
    """The last non-empty line, when it is exactly one of the verdict words."""
    for raw in reversed(text.splitlines()):
        line = raw.strip()
        if line:
            return line if line in FINAL_WORDS else None
    return None


NOOP_SERVES = {"", "true", ":", "exit 0", "echo", "sleep 0"}


def noop_serve(command: str) -> bool:
    """Whether a Serve line plainly starts nothing: empty, `true`, `:`, `exit 0`, or an
    `echo ...` / `true ...` with no server in it."""
    text = command.strip().strip(";").strip()
    if text in NOOP_SERVES:
        return True
    head = text.split(None, 1)[0] if text else ""
    return head in ("echo", "true", ":") and not any(op in text for op in ("&&", "||", "|", "$("))


def how_to_run(text: str) -> dict:
    """`**Serve.**` and `**Ready.**` from a build report, as the served-build
    script needs them. Exactly one of each, or a ContractError naming which."""
    serves = SERVE_RE.findall(text)
    readies = READY_RE.findall(text)
    if len(serves) != 1:
        raise ContractError(f"expected exactly one '**Serve.** `<command>`' line, found {len(serves)}")
    if len(readies) != 1:
        raise ContractError(
            f"expected exactly one '**Ready.** URL|COMMAND `<value>`' line, found {len(readies)}"
        )
    kind, value = readies[0]
    return {"serve": serves[0], "ready_kind": kind, "ready": value}
