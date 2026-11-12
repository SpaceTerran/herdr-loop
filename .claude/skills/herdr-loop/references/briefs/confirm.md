# Confirm brief: {{ITEM_ID}}, review {{N}}

If `{{ITEM_DIR}}/grade-{{N}}.done` exists, stop now: this brief has already been carried out.

You are the second reviewer of one backlog item in an unattended build loop. Review {{FIRST_N}} was a full review of the change at commit `{{FROM_SHA}}`, and failed it with the fix list below. A builder has since pushed a fix. You wrote neither the code nor that review, and you have not seen this item before, on purpose. Your work is narrower than a review: decide whether the fix does what the fix list asks and breaks nothing that review passed, and write a verdict. A pass merges the pull request with no person in between. A fail goes back to a builder, and the head after that gets a full review. Nobody answers a question: check what you can, and mark the rest `not_checked`.

## What you have

- The item: `{{ITEM_DIR}}/item.md`. The criteria: `{{ITEM_DIR}}/contract.md`. The verdict of review {{FIRST_N}}: `{{ITEM_DIR}}/verdict-{{FIRST_N}}.json`. What it passed stands unless the fix changed it; you do not go through the criteria again.
- Its fix list, numbered as you answer it:

{{FIX_LIST}}

- Pull request {{PR_URL}}, now at head commit `{{HEAD_SHA}}`. A checkout at that commit: `{{WORKTREE}}`. The fix is `git -C {{WORKTREE}} diff --no-renames {{FROM_SHA}} {{HEAD_SHA}}`. Run that yourself, as it is written, and read all of it. The merge reads what your own commands printed back to you: for every file the fix changes it wants one output that holds that file's part of this diff, from its `diff --git` line on, with its `index` line and every line the fix adds to the file or removes from it. So print each file's part whole. Where the diff is too long for one output, print it a file at a time (`… diff --no-renames {{FROM_SHA}} {{HEAD_SHA}} -- <path>`), not cut into pages. A diff sent to a file you do not then open, a diff of other commits, or a diff only a sub-agent read was not shown to you.
- What the fix touches, computed by a script from git, not taken from the builder:

{{DELTA_FACTS}}

- The build at the new head, as its builder said to run it, already started for you from that checkout by a script. You do not start, stop or restart it.

  {{HOW_TO_RUN}}

  {{READY_SUMMARY}}
- Your working directory is `{{EVIDENCE_DIR}}`. Screenshots (at a 1280×800 viewport), captured output and anything your verdict cites go there. Give the browser tool's screenshot a full path in that directory as its `filename` (`{{EVIDENCE_DIR}}/alerts-dark.png`): given a bare name, the browser tool saves it under `{{EVIDENCE_DIR}}/.playwright-mcp/` instead.

{{REPO_FACTS}}

{{MANAGER_NOTE}}

## How to confirm

1. **Answer every entry.** For each entry of the fix list, run the command or drive the running build yourself and see whether what it names is now right. `fixed` means you saw it right. The builder's report is its own account and shows nothing. An entry you could not exercise is `not_checked`, never `fixed`. The merge reads your session's own commands to see that the change was exercised: a request to the served build counts when its address is in the command you ran, in a script you gave an interpreter, or in a file you wrote and then ran. That counts as exercising the fix, never as a look at a page.
2. **Read the fix against what passed.** A fix is written to turn a fail into a pass, so look for what it gave up to get there. Where it touches code behind a criterion that review {{FIRST_N}} passed, exercise that criterion again. A test it deleted, skipped, loosened or rewrote to expect less needs a reason the item gives; without one it is a defect. So is a change no entry asked for that alters what the item's criteria describe.

{{LOOK}}

{{CHECKS}}

3. **Write the verdict**, then create the empty file `{{ITEM_DIR}}/grade-{{N}}.done`, then end your turn. The done file is the only thing that says you are finished.

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
  "answers": [
    {"entry": 1, "result": "fixed", "evidence": ["evidence-{{N}}/entry-1.txt"], "note": "what you ran and what you saw"}
  ],
  "delta": {"result": "clean", "note": "what the fix changes beyond the fix list, and what you ran to see it breaks nothing"},
  "fix_list": [
    {"criterion": 2, "what": "what is still wrong, in a sentence", "where": "path/to/file.py:41", "evidence": ["evidence-{{N}}/entry-1.txt"]}
  ],
  "looks": [
    {"mode": "dark", "url": "http://127.0.0.1:{{PORT}}/app/alerts", "screenshot": "evidence-{{N}}/alerts-dark.png", "result": "match", "note": "what you compared it with"}
  ],
  "look_needed": false,
  "findings": [
    {"kind": "pre-existing", "severity": "low", "confidence": 0.6, "summary": "…", "evidence": []}
  ],
  "how_run": "the commands you used to exercise the fix"
}
```

- `verdict` is exactly `PASS`, `FAIL` or `INCONCLUSIVE`. `PASS` means every entry is `fixed`, `delta` is `clean`, the fix list is empty and no look differs. Any `not_fixed`, a `delta` that `breaks`, or any entry in your own fix list makes it `FAIL`. With nothing failed and something `not_checked`, it is `INCONCLUSIVE`.
- `answers` has one entry for each entry of the fix list above, in its order: `entry` is its number there, and `result` is exactly `fixed`, `not_fixed` or `not_checked`. A missing answer makes the whole verdict unusable.
- `delta` is your answer to step 2: `clean` when the fix breaks nothing review {{FIRST_N}} passed and nothing the item asks, `breaks` when it does. The note says what the fix changes beyond the fix list and what you ran.
- **`fix_list` is what a builder must still change, and nothing else:** each entry you answered `not_fixed`, said as it stands now, and whatever the fix broke (set `criterion` to `null` for one no criterion names). Say what is wrong and where, and how you saw it; do not write the fix. Not something that was already so before this change, not a matter of taste. A `FAIL` has at least one entry. A `PASS` has none.
- **`findings` is everything else you saw.** Nobody acts on a finding during this item. Each has a `kind` (`pre-existing`, `other-files`, `environment`, `design`, or `criterion` with the criterion's number as `criterion`, or `null` there for a note about the item's own text where no criterion of the contract is wrong), a severity (`high`, `medium`, `low`) and your confidence (0 to 1). An entry of the fix list that asks for something the item does not is a finding of kind `criterion`, and its answer is `not_checked`. It must be present and may be empty.
- `looks` has one entry for each page and mode you looked at; `result` is `match` or `differs`, and a look that differs is also an entry in the fix list. Leave it empty when you looked at nothing. `look_needed` is `true` when the fix alters something a person sees.
- Every evidence path and screenshot is relative to `{{ITEM_DIR}}`, names one file (to cite a folder of screenshots, list the files in it), and must exist when you finish. Never cite a file you did not write.
- `how_run` is what you ran, not a restatement of how the build was started.
