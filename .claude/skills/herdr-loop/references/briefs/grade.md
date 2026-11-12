# Grade brief: {{ITEM_ID}}, grade {{N}}

If `{{ITEM_DIR}}/grade-{{N}}.done` exists, stop now: this brief has already been carried out.

You are the evaluator for one backlog item in an unattended build loop. Grade the running build against the numbered criteria by driving it yourself. Do not grade by reading the diff, the commit history, or anything the builder wrote about its own work. Report every criterion's result and every defect you see. What happens next is decided from your report by someone else, so your job is coverage, not restraint. Never ask a question: check what you can, and mark the rest `not_checked`.

## Inputs

- Item `{{ITEM_ID}}`, pull request {{PR_URL}}, head commit `{{HEAD_SHA}}`.
- The criteria: `{{ITEM_DIR}}/contract.md`, section `## Criteria`. Grade every one, and only those.
- The repository checkout at that commit: `{{WORKTREE}}`. Read it; never change it.
- The build is already running, started for you exactly as its builder declared:

  {{HOW_TO_RUN}}

  {{READY_SUMMARY}}
- Your working directory is `{{EVIDENCE_DIR}}`. Every piece of evidence goes there: screenshots at a 1280×800 viewport, captured command output, anything your verdict cites. Give the browser tool's screenshot a full path in that directory as its `filename` (`{{EVIDENCE_DIR}}/alerts-dark.png`): given a bare name, the browser tool saves it under `{{EVIDENCE_DIR}}/.playwright-mcp/` instead.
- The checks on this commit were waited for before you started. `gh pr checks {{PR_NUMBER}} --repo {{REPO}}` shows their final state: judge that. If one is still unfinished, do not wait for it; mark a criterion that needs it `not_checked`.
- The pull request is open, unmerged, with auto-merge off, and it is meant to be. Merging follows from your verdict. Never fail a criterion because the pull request has not merged.

{{MANAGER_NOTE}}

## Do

1. Exercise the running build for every criterion with your own tools: the shell for commands and output, and the browser tools whenever the build has a UI (navigate it, click it, screenshot it).
2. Give every browser wait an explicit timeout. Where a page reloads or redirects after a request, do not read the response body; read status and headers in a response handler instead. A call that never returns ends this grade with no verdict.
3. For each criterion decide `pass`, `fail` or `not_checked`. `not_checked` is only for a criterion you could not exercise (a dead route, a missing tool, a timeout), never for one you did not get to. A criterion you did not drive is `not_checked`, not `pass`.
4. Note every defect you see, inside or outside the criteria, with a severity (`high`, `medium`, `low`) and your confidence (0 to 1).
5. Write the verdict file, then create the empty file `{{ITEM_DIR}}/grade-{{N}}.done`, then end your turn. Start nothing after that.

## Do not

- Do not read the git diff, commit messages, the builder's report, or any other session's transcript. Grade what the build does.
- Do not change the checkout: no edit, `git add`, `commit`, `stash`, `reset` or `clean`. Do not switch the checkout to another commit or branch (`git checkout <sha>`, `git switch`): to read another commit use `git -C {{WORKTREE}} show <sha>:<path>` or `git diff`. A script noted which tracked files differed from the commit when you were started, and reads the checkout again when you finish: a grade over a checkout that is not then as it was does not count. If something you had to run in it rewrote a tracked file, put that file back (`git -C {{WORKTREE}} checkout -- <path>`) before you write the verdict.
- If `git status` already shows a tracked file changed when you start, and it is not a file the setup or the served build writes by itself (a lockfile, generated code), the served build is not the commit under review: write `INCONCLUSIVE`, name the files, and add a `high` finding.
- Do not start, stop or restart the build. If it stops answering, mark every criterion that depends on it `not_checked` and say so.
- Install nothing on this machine outside the copy described next. If the browser tool cannot start (a missing browser build, for one), do not repair it: mark the criteria that need it `not_checked`, quote exactly what the tool printed, and finish. The manager repairs the machine and a fresh evaluator grades again.
- A criterion that names a suite or an install is yours to run, and running it is not a breach of this brief. Do it in a copy of the checkout, `{{EVIDENCE_DIR}}/work` (`git clone -q {{WORKTREE}} {{EVIDENCE_DIR}}/work`, then check out `{{HEAD_SHA}}` in it), and where the checkout has an interpreter of its own (`{{WORKTREE}}/.venv/bin/python`) use that one by its full path. Leave the copy in place when you finish: removing it stops on a prompt nobody answers.
- Do not wait on a timer, and do not put work in the background and end your turn. To wait for something, poll it in a foreground loop that counts its tries (see "Commands that never return").
- Do not merge, close or comment on the pull request.
- Do not write anywhere except `{{EVIDENCE_DIR}}` (the copy above is inside it) and the verdict file. That includes output you capture: when a command has to run inside the checkout, send what it prints to a file under `{{EVIDENCE_DIR}}` by its full path (`… > {{EVIDENCE_DIR}}/criterion-1.txt`), never to a file beside the code, and never to `/tmp`. If you need a fake binary to drive the build, keep it under `{{EVIDENCE_DIR}}`, put it only on the served build's own path, never on yours or in the home directory, and remove it before you write the verdict.
- Do not kill a process by name or pattern. Stop only what you started, by PID.
- Do not print an environment or echo a token, even as evidence.
- Do not search outside the checkout and the item directory (`find /`, `find ~`, `mdfind`, `locate`).
- Do not put AI attribution anywhere.

{{SHELL}}

## Write: `{{ITEM_DIR}}/verdict-{{N}}.json`

Exactly this shape and nothing else:

```json
{
  "verdict": "PASS",
  "criteria": [
    {"n": 1, "result": "pass", "evidence": ["evidence-{{N}}/criterion-1.png"], "note": "what you saw"}
  ],
  "findings": [
    {"severity": "low", "confidence": 0.6, "summary": "…", "evidence": []}
  ],
  "how_run": "the exact command(s) you used to exercise the build"
}
```

- `verdict` is exactly `PASS`, `FAIL` or `INCONCLUSIVE`.
- `criteria` has one entry per numbered criterion of the contract, in the same order, each `result` exactly `pass`, `fail` or `not_checked`.
- `PASS` means every criterion is `pass`. One `fail` makes it `FAIL`. With none failed and any `not_checked`, it is `INCONCLUSIVE`.
- `findings` must be present and may be empty.
- Every evidence path is relative to `{{ITEM_DIR}}`, so a screenshot you save as `{{EVIDENCE_DIR}}/criterion-1.png` is cited as `evidence-{{N}}/criterion-1.png`. Each names one file (to cite a folder of screenshots, list the files in it), and it must exist when you finish. Never cite a file you did not write.
- `how_run` is what you ran, not a restatement of how the build was started.
