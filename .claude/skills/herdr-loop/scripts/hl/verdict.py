"""`verdict-<n>.json`: is it a verdict, and what does it actually say?

Shape and the not-checked downgrade are ported from LoopRunner's
`looprunner/verdict.py`. Unlike the runner, this reports every problem at
once: the manager passes the list to the evaluator, which is still there to
fix its own file.

    { "verdict": "PASS" | "FAIL" | "INCONCLUSIVE",
      "criteria": [ { "n", "result": "pass" | "fail" | "not_checked",
                      "evidence": [paths], "note" } ],
      "findings": [ { "severity": "high" | "medium" | "low", "confidence": 0-1,
                      "summary", "evidence": [paths] } ],
      "how_run": "<the command actually used>" }

Evidence paths are relative to the item directory, where the verdict file sits.

That is a grade's verdict: the evaluator drove the build and did not read
the diff (style `blind`). A review's verdict (style `code`) comes from a
reviewer that read the code and exercised the change, and adds:

      "fix_list": [ { "criterion": n | null, "what", "where", "evidence": [paths] } ],
      "looks":    [ { "mode", "url", "screenshot": path, "result": "match" | "differs", "note" } ],
      "look_needed": true | false

`fix_list` is what the builder must change for the item to pass, and nothing
else: every entry costs a rework. A FAIL has one; a PASS has none. Everything
else the reviewer saw is a finding, and in a review each finding says what
kind it is (`"kind"`): something that was so before the change, something in
files the item may not write, the test host, the design the item specifies,
or a criterion that is itself wrong (with `"criterion": n`). A finding of kind
`criterion` with no number (`null`, or none given) names no criterion of the
contract: it is a note about the item's own text (its Scope, or an Accept line
a correction replaced), kept for follow-up like any other finding, and no
criterion is taken to a correction for it. Of 40 findings
the first program's evaluators reported, 21 were the test host repeating
itself and none was a defect in what its item changed.
`looks` are the reviewer's looks at served pages in a real browser, one per
page and mode, each with its picture. `look_needed` says the change alters
something a person sees, whether or not the item was routed that way.

In a review a criterion may also be `checks`: it only restates what the
repository's required checks run, and the merge gate holds those green on
the commit that merges, so the reviewer does not run them again.

A delta confirm's verdict is a third shape. The confirm follows a review
that failed with a fix list, and is given only the fix made since. It does not
go through the criteria again; it answers that fix list, entry by entry:

    { "verdict": "PASS" | "FAIL" | "INCONCLUSIVE",
      "answers": [ { "entry": k, "result": "fixed" | "not_fixed" | "not_checked",
                     "evidence": [paths], "note" } ],
      "delta":   { "result": "clean" | "breaks", "note" },
      "fix_list", "looks", "look_needed", "findings", "how_run" }

`answers` has one entry for each entry of the failed review's fix list, in
its order, numbered from 1. `delta` says whether the fix breaks anything
the failed review passed. `fix_list` is what is still to change. A PASS has
every entry `fixed`, a `clean` delta, an empty fix list and no look that
differs. Which review is a confirm is read from the attempt log beside the
verdict, never from the verdict itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import contractfmt
from .common import absolute, emit, read_json, sha256_text

VERDICT_WORDS = ("PASS", "FAIL", "INCONCLUSIVE")
CRITERION_RESULTS = ("pass", "fail", "not_checked")
# What the required checks prove, the merge gate enforces; a review does not run them again.
BY_CHECKS = "checks"
STYLES = ("blind", "code")
LOOK_RESULTS = ("match", "differs")
# What a review's finding is about. None of them is the builder's to fix in this item.
FINDING_KINDS = ("pre-existing", "other-files", "environment", "design", "criterion")
FINDING_SEVERITIES = ("high", "medium", "low")
DRIVING_TOOL_PREFIXES = ("Bash", "mcp__playwright__")
# The kind a delta confirm has in the attempt log (`items.CONFIRM`; this module reads the log without the rest of a run).
CONFIRM = "confirm"
ANSWER_RESULTS = ("fixed", "not_fixed", "not_checked")
DELTA_RESULTS = ("clean", "breaks")


def _review_shape_problems(data: dict) -> list[str]:
    """The parts only a review's verdict has."""
    problems: list[str] = []
    fixes = data.get("fix_list")
    if not isinstance(fixes, list):
        problems.append("fix_list must be a list (empty on a PASS)")
        fixes = []
    for f in fixes:
        if not isinstance(f, dict):
            problems.append(f"each fix-list entry must be an object, got {f!r}")
            continue
        if not isinstance(f.get("what"), str) or not f["what"].strip():
            problems.append("a fix-list entry is missing `what`: say what is wrong")
        criterion = f.get("criterion")
        if criterion is not None and (not isinstance(criterion, int) or isinstance(criterion, bool)):
            problems.append(f"a fix-list entry has criterion {criterion!r}; use the criterion's number, or null for a defect no criterion names")
        if "evidence" in f and not isinstance(f["evidence"], list):
            problems.append("a fix-list entry's evidence must be a list of paths")
    if data.get("verdict") == "FAIL" and not fixes:
        problems.append("a FAIL needs a fix list: say what the builder must change")

    looks = data.get("looks", [])
    if not isinstance(looks, list):
        problems.append("looks must be a list")
        looks = []
    for look in looks:
        if not isinstance(look, dict):
            problems.append(f"each look must be an object, got {look!r}")
            continue
        for key in ("mode", "url", "screenshot"):
            if not isinstance(look.get(key), str) or not look[key]:
                problems.append(f"a look is missing `{key}`")
        if look.get("result") not in LOOK_RESULTS:
            problems.append(f"a look has result {look.get('result')!r}; use match or differs")
    if "look_needed" in data and not isinstance(data["look_needed"], bool):
        problems.append("look_needed must be true or false")
    for f in data.get("findings") if isinstance(data.get("findings"), list) else []:
        if not isinstance(f, dict):
            continue
        if f.get("kind") not in FINDING_KINDS:
            problems.append(f"a finding has kind {f.get('kind')!r}; use one of {', '.join(FINDING_KINDS)}")
        elif f["kind"] == "criterion" and f.get("criterion") is not None and not _is_number(f["criterion"]):
            # None is a note about the item's own text (`criterion_notes`); anything else must be a criterion's number.
            problems.append(f"a finding of kind criterion has criterion {f['criterion']!r}: give the criterion's number, or null for a note about the item's own text that names no criterion")
    return problems


def _is_number(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _criterion_findings(findings: list) -> tuple[list[int], int]:
    """The criteria a review's findings say are themselves wrong, which the
    manager takes to a correction; and how many `criterion` findings name no
    criterion. Those are notes about the item's own text: reviewers write
    them after a correction, comparing the item's Accept text with the
    contract that replaced it, and a whole PASS was refused for one four
    times in a help-desk feature program (three items, one of them twice)."""
    about = [f for f in findings if isinstance(f, dict) and f.get("kind") == "criterion"]
    return sorted({f["criterion"] for f in about if _is_number(f.get("criterion"))}), sum(1 for f in about if f.get("criterion") is None)


def shape_problems(data, style: str = "blind") -> list[str]:
    if not isinstance(data, dict):
        return ["the file must hold one JSON object"]
    results = CRITERION_RESULTS + ((BY_CHECKS,) if style == "code" else ())
    problems: list[str] = []
    if data.get("verdict") not in VERDICT_WORDS:
        problems.append(f"verdict must be one of {', '.join(VERDICT_WORDS)}, got {data.get('verdict')!r}")

    criteria = data.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        problems.append("criteria must be a non-empty list")
        criteria = []
    seen: set[int] = set()
    for c in criteria:
        if not isinstance(c, dict):
            problems.append(f"each criterion must be an object, got {c!r}")
            continue
        n = c.get("n")
        if not isinstance(n, int) or isinstance(n, bool):
            problems.append(f"criterion is missing an integer n: {c!r}")
            continue
        if n in seen:
            problems.append(f"criterion {n} appears more than once")
        seen.add(n)
        if c.get("result") not in results:
            problems.append(f"criterion {n} has result {c.get('result')!r}; use {', '.join(results[:-1])} or {results[-1]}")
        if not isinstance(c.get("evidence"), list):
            problems.append(f"criterion {n} is missing an evidence list")
        if not isinstance(c.get("note"), str):
            problems.append(f"criterion {n} is missing a note")

    problems += _findings_and_run_problems(data)
    if style == "code":
        problems += _review_shape_problems(data)
    return problems


def _findings_and_run_problems(data: dict) -> list[str]:
    """The parts every verdict has, whatever it judged."""
    problems: list[str] = []
    findings = data.get("findings")
    if not isinstance(findings, list):
        problems.append("findings must be a list (it may be empty)")
        findings = []
    for f in findings:
        if not isinstance(f, dict):
            problems.append(f"each finding must be an object, got {f!r}")
            continue
        if f.get("severity") not in FINDING_SEVERITIES:
            problems.append(f"a finding has severity {f.get('severity')!r}; use high, medium or low")
        confidence = f.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            problems.append("a finding is missing a numeric confidence")
        if not f.get("summary"):
            problems.append("a finding is missing a summary")
        if not isinstance(f.get("evidence"), list):
            problems.append("a finding is missing an evidence list")

    if not isinstance(data.get("how_run"), str) or not data.get("how_run"):
        problems.append("how_run must be a non-empty string")
    return problems


def confirm_shape_problems(data, entries: int) -> list[str]:
    """What is wrong with a delta confirm's verdict. `entries` is how many
    entries the fix list it answers has: every one needs its answer."""
    if not isinstance(data, dict):
        return ["the file must hold one JSON object"]
    problems: list[str] = []
    if data.get("verdict") not in VERDICT_WORDS:
        problems.append(f"verdict must be one of {', '.join(VERDICT_WORDS)}, got {data.get('verdict')!r}")
    if "criteria" in data:
        # Nothing reads such a list in a confirm, so nothing in it would be held to anything.
        problems.append("a confirm answers the fix list and carries no `criteria`: what you saw of a criterion goes in an answer's note or in a finding")

    answers = data.get("answers")
    if not isinstance(answers, list):
        problems.append("answers must be a list, with one answer for each entry of the fix list")
        answers = []
    got = []
    for a in answers:
        if not isinstance(a, dict):
            problems.append(f"each answer must be an object, got {a!r}")
            continue
        k = a.get("entry")
        if not isinstance(k, int) or isinstance(k, bool):
            problems.append(f"an answer is missing the number of the entry it answers: {a!r}")
            continue
        got.append(k)
        if a.get("result") not in ANSWER_RESULTS:
            problems.append(f"the answer to entry {k} has result {a.get('result')!r}; use {', '.join(ANSWER_RESULTS[:-1])} or {ANSWER_RESULTS[-1]}")
        if not isinstance(a.get("evidence"), list):
            problems.append(f"the answer to entry {k} is missing an evidence list")
        if not isinstance(a.get("note"), str) or not a["note"].strip():
            problems.append(f"the answer to entry {k} is missing a note: say what you ran and what you saw")
    expected = list(range(1, entries + 1))
    if got != expected:
        problems.append(f"answers must be exactly entries {expected} of the fix list, in that order, got {got}")

    delta = data.get("delta")
    if not isinstance(delta, dict) or delta.get("result") not in DELTA_RESULTS:
        problems.append("delta must be an object whose result is clean or breaks: say whether the fix breaks anything")
    elif not isinstance(delta.get("note"), str) or not delta["note"].strip():
        problems.append("delta is missing a note: say what the fix changes beyond the fix list and what you ran")
    return problems + _findings_and_run_problems(data) + _review_shape_problems(data)


def effective_verdict(data: dict, style: str = "blind") -> tuple[str, str | None]:
    """The word policy acts on. A PASS is a pass only when every criterion
    passed; a FAIL names at least one failed criterion. A review's PASS
    also has an empty fix list and no look that differs from its reference,
    and a review's FAIL may rest on its fix list alone: a defect that
    breaks what the item asks need not have a criterion of its own."""
    results = {c["n"]: c["result"] for c in data["criteria"]}
    not_checked = sorted(n for n, r in results.items() if r == "not_checked")
    failed = sorted(n for n, r in results.items() if r == "fail")
    word = data["verdict"]
    review = style == "code"
    fixes = len(data.get("fix_list") or []) if review else 0
    if word == "PASS" and failed:
        return "FAIL", f"verdict says PASS but criteria {failed} failed"
    if word == "PASS" and fixes:
        return "FAIL", f"verdict says PASS but the fix list has {fixes} entries"
    if word == "PASS" and review and any(look.get("result") == "differs" for look in data.get("looks") or []):
        return "FAIL", "verdict says PASS but a look differs from its reference"
    if word == "PASS" and not_checked:
        return "INCONCLUSIVE", f"verdict says PASS but criteria {not_checked} are not_checked"
    if word == "PASS" and review and all(r == BY_CHECKS for r in results.values()):
        return "INCONCLUSIVE", "verdict says PASS but every criterion is left to the required checks: nothing was exercised"
    if word == "PASS" and review and data.get("look_needed") and not data.get("looks"):
        return "INCONCLUSIVE", "verdict says PASS and that the change alters something a person sees, but records no look"
    if word == "FAIL" and not failed and not fixes:
        return "INCONCLUSIVE", "verdict says FAIL but no criterion is marked fail"
    return word, None


def effective_confirm(data: dict) -> tuple[str, str | None]:
    """The word policy acts on for a delta confirm. A PASS is a pass only
    when every entry of the fix list is fixed, the fix breaks nothing, the
    fix list is empty and no look differs. (A FAIL always has its fix list:
    the shape asks for one.)"""
    results = {a["entry"]: a["result"] for a in data["answers"]}
    not_fixed = sorted(k for k, r in results.items() if r == "not_fixed")
    not_checked = sorted(k for k, r in results.items() if r == "not_checked")
    fixes = len(data["fix_list"])
    if data["verdict"] != "PASS":
        return data["verdict"], None
    if not_fixed:
        return "FAIL", f"verdict says PASS but entries {not_fixed} of the fix list are not fixed"
    if data["delta"]["result"] == "breaks":
        return "FAIL", "verdict says PASS but the fix is said to break something"
    if fixes:
        return "FAIL", f"verdict says PASS but the fix list has {fixes} entries"
    if any(look.get("result") == "differs" for look in data.get("looks") or []):
        return "FAIL", "verdict says PASS but a look differs from its reference"
    if not_checked:
        return "INCONCLUSIVE", f"verdict says PASS but entries {not_checked} of the fix list are not_checked"
    if data.get("look_needed") and not data.get("looks"):
        return "INCONCLUSIVE", "verdict says PASS and that the fix alters something a person sees, but records no look"
    return "PASS", None


def digest(data) -> str:
    """A verdict as one value. A delta confirm keeps the digest of the failed
    review's verdict it started from: its fix list is what the confirm
    answers and its looks are what the confirm looks at again, so neither
    may change under it."""
    return sha256_text(json.dumps(data, sort_keys=True))


def file_sha(item_dir, n: int) -> str | None:
    """The verdict file of review `n` as one value, or None when it is not
    there. `hl-flow reviewed` keeps this on the review's attempt when it
    first reads the review: what is read from the file later counts only
    while the file is still that one."""
    path = Path(item_dir) / f"verdict-{n}.json"
    return sha256_text(path.read_text()) if path.is_file() else None


def confirm_of(item_dir, n: int) -> dict | None:
    """The attempt record of review `n` when that review is a delta confirm."""
    path = Path(item_dir) / "attempts.json"
    if not path.exists():
        return None
    return next(
        (e for e in read_json(path)["log"] if e.get("phase") == "grade" and e.get("n") == n and not e.get("voided") and e.get("kind") == CONFIRM), None
    )


def _evidence_problems(item_dir: Path, data: dict) -> list[str]:
    problems: list[str] = []
    root = item_dir.resolve()
    entries = [(f"criterion {c['n']}", c["evidence"]) for c in data.get("criteria") or []]
    entries += [(f"the answer to entry {a['entry']}", a["evidence"]) for a in data.get("answers") or []]
    entries += [("a finding", f["evidence"]) for f in data["findings"]]
    entries += [("a fix-list entry", f.get("evidence") or []) for f in data.get("fix_list") or []]
    entries += [("a look", [look["screenshot"]]) for look in data.get("looks") or []]
    for label, paths in entries:
        for p in paths:
            if not isinstance(p, str) or not p:
                problems.append(f"{label} cites an evidence entry that is not a path: {p!r}")
                continue
            target = (item_dir / p).resolve()
            if root != target and root not in target.parents:
                problems.append(f"{label} cites {p!r}, which is outside the item directory")
            elif target.is_dir():
                # Still refused: a folder says nothing of which file shows what, and an empty one would pass. Said as what it is.
                problems.append(f"{label} cites {p!r}, which is a directory: an evidence entry names one file, so list the files in it")
            elif not target.is_file():
                problems.append(f"{label} cites {p!r}, which does not exist")
    return problems


def driving_calls(transcript: Path) -> int:
    """Tool calls in a Claude Code transcript that could have driven a build:
    a shell command or a browser action. Zero means the verdict was written
    from reading alone."""
    count = 0
    with open(transcript, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and str(block.get("name", "")).startswith(DRIVING_TOOL_PREFIXES)
                ):
                    count += 1
    return count


def check(item_dir, n: int, transcript=None, style: str = "blind") -> dict:
    """Everything wrong with attempt `n`'s verdict, and the word it amounts to.

    `problems` empty means the file is a usable verdict; `effective` is then
    PASS, FAIL or INCONCLUSIVE. With problems, `effective` is None. `style`
    is the run's: `code` for a review's verdict.
    """
    if style not in STYLES:
        raise ValueError(f"style {style!r} is not one of {', '.join(STYLES)}")
    item_dir = Path(item_dir)
    path = item_dir / f"verdict-{n}.json"
    out = {"file": path.name, "raw": None, "effective": None, "downgraded": None, "problems": []}
    if not path.exists():
        out["problems"].append(f"{path.name} does not exist")
        return out
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        out["problems"].append(f"{path.name} is not valid JSON: {exc}")
        return out

    confirm = confirm_of(item_dir, n) if style == "code" else None
    if confirm:
        return _check_confirm(item_dir, data, confirm, transcript, out)
    out["problems"] += shape_problems(data, style)
    if out["problems"]:
        return out
    out["raw"] = data["verdict"]
    if style == "code":
        out["fix_list"] = len(data["fix_list"])
        out["by_checks"] = sorted(c["n"] for c in data["criteria"] if c["result"] == BY_CHECKS)
        # Criteria the reviewer says are themselves wrong: the manager takes these to a correction, not to the builder.
        out["criteria_in_question"], out["criterion_notes"] = _criterion_findings(data["findings"])

    try:
        contract = contractfmt.parse_criteria((item_dir / "contract.md").read_text())
    except (OSError, contractfmt.ContractError) as exc:
        out["problems"].append(f"contract.md cannot be read to compare criteria: {exc}")
        return out
    expected = [number for number, _ in contract]
    got = [c["n"] for c in data["criteria"]]
    if got != expected:
        out["problems"].append(f"criteria must be exactly {expected} in that order, got {got}")
    for f in data.get("fix_list") or [] if style == "code" else []:
        if f.get("criterion") is not None and f["criterion"] not in expected:
            out["problems"].append(f"a fix-list entry names criterion {f['criterion']}, which the contract does not have")

    out["problems"] += _evidence_problems(item_dir, data) + _transcript_problems(transcript, out)
    if not out["problems"]:
        out["effective"], out["downgraded"] = effective_verdict(data, style)
    return out


def _transcript_problems(transcript, out: dict) -> list[str]:
    if transcript is None:
        return []
    try:
        calls = driving_calls(Path(transcript))
    except OSError as exc:
        return [f"transcript {transcript} cannot be read: {exc}"]
    out["driving_calls"] = calls
    return [] if calls else ["the evaluator's transcript shows no shell or browser call: the build was not driven"]


def _check_confirm(item_dir: Path, data, confirm: dict, transcript, out: dict) -> dict:
    """A delta confirm's verdict, read against the fix list it was given:
    the failed review's own, whose verdict must still be the one the
    confirm started from."""
    first = item_dir / f"verdict-{confirm.get('confirms')}.json"
    try:
        failed = json.loads(first.read_text())
        fixes = failed["fix_list"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        out["problems"].append(f"{first.name}, whose fix list this confirm answers, cannot be read: {exc}")
        return out
    if not isinstance(fixes, list) or digest(failed) != confirm.get("verdict_sha"):
        out["problems"].append(f"{first.name} is not the verdict this confirm was given to answer: the file changed after the confirm started")
        return out
    out["problems"] += confirm_shape_problems(data, len(fixes))
    if out["problems"]:
        return out
    out["raw"] = data["verdict"]
    results = {a["entry"]: a["result"] for a in data["answers"]}
    out.update(confirms=confirm["confirms"], answers={word: sorted(k for k, r in results.items() if r == word) for word in ANSWER_RESULTS})
    out.update(delta=data["delta"]["result"], fix_list=len(data["fix_list"]), by_checks=[])
    out["criteria_in_question"], out["criterion_notes"] = _criterion_findings(data["findings"])
    try:
        expected = [number for number, _ in contractfmt.parse_criteria((item_dir / "contract.md").read_text())]
    except (OSError, contractfmt.ContractError) as exc:
        out["problems"].append(f"contract.md cannot be read to compare criteria: {exc}")
        return out
    for f in data["fix_list"]:
        if f.get("criterion") is not None and f["criterion"] not in expected:
            out["problems"].append(f"a fix-list entry names criterion {f['criterion']}, which the contract does not have")
    out["problems"] += _evidence_problems(item_dir, data) + _transcript_problems(transcript, out)
    if not out["problems"]:
        out["effective"], out["downgraded"] = effective_confirm(data)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hl-verdict", description=__doc__.split("\n\n")[0])
    p.add_argument("--item-dir", required=True, type=absolute)
    p.add_argument("--n", required=True, type=int, help="the grade attempt number")
    p.add_argument("--transcript", help="the evaluator session's transcript (.jsonl)")
    p.add_argument("--style", choices=list(STYLES), help="blind: a grade's verdict; code: a review's (default: the run's own)")
    args = p.parse_args(argv)
    style = args.style
    if style is None:
        # The item directory is <run>/items/<ID>; the run says which kind of verdict it takes.
        from . import items  # here, not at the top: nothing else in this module needs a run

        run_path = Path(args.item_dir).resolve().parent.parent / "run.json"
        style = items.policy(items.load_run(run_path.parent))["review"] if run_path.exists() else "blind"
    result = check(args.item_dir, args.n, transcript=args.transcript, style=style)
    emit(result)
    return 2 if result["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
