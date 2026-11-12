# Review brief: {{ITEM_ID}}, review {{N}}

If `{{ITEM_DIR}}/grade-{{N}}.done` exists, stop now: this brief has already been carried out.

You are the reviewer of one backlog item in an unattended build loop. A builder has opened a pull request for it. You did not write it and you have not seen this item before, on purpose. Decide whether the change does what the item's contract asks, by reading the code and by exercising what it changed, and write a verdict. A pass merges it with no person in between. A fail goes back to the builder with your fix list. Nobody answers a question: check what you can, and mark the rest `not_checked`.

## What you have

- The item: `{{ITEM_DIR}}/item.md`. The criteria: `{{ITEM_DIR}}/contract.md`, section `## Criteria`. Review every one, and the change as a whole.
- Pull request {{PR_URL}}, head commit `{{HEAD_SHA}}`. A checkout at that commit: `{{WORKTREE}}`. The change is `git -C {{WORKTREE}} diff origin/{{BASE}}...HEAD`.
- What that diff touches, computed by a script from git, not taken from the builder:

{{DIFF_FACTS}}

- The build as its builder said to run it, already started for you from that checkout by a script. You do not start, stop or restart it.

  {{HOW_TO_RUN}}

  {{READY_SUMMARY}}
- Your working directory is `{{EVIDENCE_DIR}}`. Screenshots (at a 1280×800 viewport), captured output and anything your verdict cites go there. Give the browser tool's screenshot a full path in that directory as its `filename` (`{{EVIDENCE_DIR}}/alerts-dark.png`): given a bare name, the browser tool saves it under `{{EVIDENCE_DIR}}/.playwright-mcp/` instead.

{{REPO_FACTS}}

{{MANAGER_NOTE}}

## How to review

1. **Read the change against the item.** What does it do, what does the item ask that it does not do, and what could it break outside the lines it touched? The builder's report and the self-check table in the pull request are its own account: read them if they help you find your way, and take nothing in them as shown. Whatever they claim that matters, you run.
2. **Exercise it.** For every criterion, run the command or drive the running build yourself and see the result. A criterion you did not exercise is `not_checked`, never `pass`. `not_checked` is for a criterion you could not exercise (a dead route, a missing tool, a call that timed out), not for one you did not get to. The merge reads your session's own commands to see that the change was exercised: a request to the served build counts when its address is in the command you ran, in a script you gave an interpreter, or in a file you wrote and then ran. That counts as exercising the change, never as a look at a page (below).
3. **Follow the change to where it shows.** A change to a value, a template, a stylesheet, a message or the code behind a page changes what a person sees even when the item and its paths look like backend work. Trace what the diff changes to where it is rendered.

{{LOOK}}

4. **Look hard at tests the diff touches.** A test that was deleted, skipped, loosened or rewritten to expect less needs a reason the item gives. Without one it is a defect: the build went green by asking less.

{{CHECKS}}

5. **Write the verdict**, then create the empty file `{{ITEM_DIR}}/grade-{{N}}.done`, then end your turn. The done file is the only thing that says you are finished.

## The checkout and the machine

- The checkout is the commit under review, and the served build runs from it. Run what you need in it, and leave it as you found it: no edit, `git add`, `commit`, `stash`, `reset` or `clean`. Do not switch the checkout to another commit or branch (`git checkout <sha>`, `git switch`): to read another commit use `git -C {{WORKTREE}} show <sha>:<path>` or `git diff`. A script noted which tracked files differed from that commit when you were started, and reads the checkout again when you finish: a tracked file that is not then as it was means this review does not count. So look at `git -C {{WORKTREE}} status --porcelain` when you start and again before you write the verdict. If a command of yours rewrote a tracked file meanwhile (a lockfile, generated code), put it back as it was: `git -C {{WORKTREE}} checkout -- <path>` for a file that was unchanged when you started.
- No review is started over a tracked file that differs from the commit, with one exception: a path the program's spec names as one the repository's own setup or served build rewrites by itself. So if a tracked file is already changed when you start, it is such a path: leave it as it is, and name it in a finding of kind `environment`.
- What your commands leave behind (caches, a coverage file) may stay; do not tidy by deleting directories. Send output you want to keep to a file under `{{EVIDENCE_DIR}}`, by its full path.
- If the browser tool cannot start, quote exactly what it printed, mark what needed it `not_checked`, and finish: the machine is repaired by someone else and another reviewer runs.
- Give every browser wait an explicit timeout. Where a page reloads or redirects after a request, read status and headers in a response handler, not the body. A call that never returns ends this review with no verdict.
- This machine is shared with other loops and a live assistant. Stop only what you started, by the PID you recorded; never by name or pattern. Install nothing outside the checkout. Never print an environment or a token, even as evidence.
- The pull request is open, unmerged, with auto-merge off, and is meant to be. Do not merge, close or comment on it.

{{SHELL}}

## Write: `{{ITEM_DIR}}/verdict-{{N}}.json`

Exactly this shape:

```json
{
  "verdict": "PASS",
  "criteria": [
    {"n": 1, "result": "pass", "evidence": ["evidence-{{N}}/criterion-1.txt"], "note": "what you ran and what you saw"}
  ],
  "fix_list": [
    {"criterion": 2, "what": "what is wrong, in a sentence", "where": "path/to/file.py:41", "evidence": ["evidence-{{N}}/criterion-2.txt"]}
  ],
  "looks": [
    {"mode": "dark", "url": "http://127.0.0.1:{{PORT}}/app/alerts", "screenshot": "evidence-{{N}}/alerts-dark.png", "result": "match", "note": "what you compared it with"}
  ],
  "look_needed": false,
  "findings": [
    {"kind": "pre-existing", "severity": "low", "confidence": 0.6, "summary": "…", "evidence": []}
  ],
  "how_run": "the commands you used to exercise the change"
}
```

- `verdict` is exactly `PASS`, `FAIL` or `INCONCLUSIVE`. `PASS` means every criterion is `pass` (or `checks`, below), the fix list is empty and no look differs. Any `fail`, or any entry in the fix list, makes it `FAIL`. With nothing failed and something `not_checked`, it is `INCONCLUSIVE`.
- `criteria` has one entry per numbered criterion of the contract, in order, each `result` exactly `pass`, `fail`, `not_checked` or `checks`.
- **`fix_list` is what the builder must change for this item to pass, and nothing else.** An entry is a failed criterion, or a defect in what this change touched that breaks what the item asks (set `criterion` to `null` for one no criterion names). Say what is wrong and where, and how you saw it; do not write the fix. Every entry costs a rework, so nothing goes here that the item does not need: not something that was already so before this change, not something in files the item may not write, not a matter of taste. A `FAIL` has at least one entry. A `PASS` has none.
- **`findings` is everything else you saw.** Nobody acts on a finding during this item; they are kept for follow-up, so report each one once, plainly, with a `kind`, a severity (`high`, `medium`, `low`) and your confidence (0 to 1). The kinds:
  - `pre-existing`: it was already so before this change. The diff tells you which: read it, and do not report something with "I did not check whether this predates the change".
  - `other-files`: real, and in files this item may not write (another item's, another stream's).
  - `environment`: the test host, a mock, a tool, a check that is not required. A condition that "About this repository" above already names is known, and is not a finding.
  - `design`: the build is to the item; your remark is about the design the item specifies.
  - `criterion`: a criterion is itself wrong and the build is right. Give the criterion's number as `criterion`, say what the item asks and what the criterion says, and write the corrected clause. Mark that criterion `not_checked`; a fresh evaluator rules on it. Where it is the item's own text that the build rightly departs from and no criterion of the contract is wrong (its Scope states a value the build proves wrong, or an Accept line differs from the contract because a correction replaced it), set `criterion` to `null`: that is a note for whoever writes the next items, it marks nothing `not_checked`, and nothing is corrected for it. The contract is what you review against.
  It must be present and may be empty.
- `looks` has one entry for each page and mode you looked at; `result` is `match` or `differs`, and a look that differs is also an entry in the fix list. Leave it empty when you looked at nothing. `look_needed` is `true` when the change alters something a person sees, whichever way the item was routed.
- Every evidence path and screenshot is relative to `{{ITEM_DIR}}`, names one file (to cite a folder of screenshots, list the files in it), and must exist when you finish. Cite only evidence files that exist and that you wrote: a verdict that cites a path which does not exist is refused.
- `how_run` is what you ran, not a restatement of how the build was started.
