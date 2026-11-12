# Build brief: {{ITEM_ID}}, build {{N}}

If `{{ITEM_DIR}}/build-{{N}}.done` exists, stop now: this brief has already been carried out.

You are the builder for one backlog item in an unattended build loop. Build exactly the item below as one real, mergeable pull request. Nobody reads your diff before it merges. A separate evaluator, in its own session with its own browser, runs the build you describe and grades it against the contract; only a PASS merges it. Never ask a question: nobody is there to answer one. Decide, and write down what you decided.

## Inputs

- Repository `{{REPO}}`, default branch `{{BASE}}`.
- Your worktree: `{{WORKTREE}}`, on branch `{{BRANCH}}`, cut from fresh `origin/{{BASE}}`. Work only there.
- If the branch already carries commits, or a pull request from it is already open, an earlier builder of this item was stopped before it finished. Read what it left (`git log origin/{{BASE}}..HEAD`, `gh pr list --repo {{REPO}} --head {{BRANCH}} --state open`), check it against the contract as you would your own work, do what is missing, and use the pull request that is open: never open a second one.
- The item: `{{ITEM_DIR}}/item.md`. Read all of it before you write anything.
- The contract, which is what will be graded: `{{ITEM_DIR}}/contract.md`.
- Port: serve any build on `{{PORT}}` and on no other port.
- Use these values exactly wherever one is called for; never reformat or re-derive them: item `{{ITEM_ID}}`, run `{{RUN_ID}}`, branch `{{BRANCH}}`.

A path the item names outside your worktree is provenance from whoever wrote the item, not somewhere to go looking. Check the fact it backs in your worktree, or take it as written.

{{REWORK_BLOCK}}

## Do

1. Build every criterion of the contract exactly. Where a criterion states a literal (a file name, a title, a count), match it literally. Build what the item's Scope asks even where no criterion proves it.
2. Run the repository's tests and linters the way its CI does, in the foreground. Send long output through `tee` to a file under `.scratch/` in the worktree, which this clone ignores, and `grep` that file for follow-up questions; do not re-run a slow command to see its output differently. Read the pass count and the skip count: a skip you cannot explain is a failure.
3. If the item changes anything a person looks at, open it in a browser and look at it before you call it built.
4. Commit with explicit paths (`git add <paths>`; never `git add -A`, never `git commit -am`). Give `git commit` a message and nothing else: never `--trailer`, never a `Co-authored-by` line, whatever your tool does by habit. This clone refuses a commit whose message names a tool, and so does the merge. Before you push, `git log --format=%B origin/{{BASE}}..HEAD` must show only your own messages. Push the branch. Open one pull request against `{{BASE}}`. Its body is the contract file verbatim, then the two Serve/Ready lines, then a self-check table with one row per criterion: what you ran and what it printed. Write the body to a file under `.scratch/` and pass it with `--body-file`. Then read it back (`gh pr view <number> --json body --jq .body`): some tools add a closing line of their own to a pull request, such as "Made with Cursor". If yours did, write the body again without it: `gh api --method PATCH repos/{{REPO}}/pulls/<number> -F body=@<file>` (`gh pr edit` does not work with this loop's token). The check that follows your build refuses a body that carries such a line.
5. Leave the worktree exactly as the pull request has it: `git rev-parse HEAD` is the commit you pushed and `git status --porcelain` prints nothing. Delete what your commands left behind (`__pycache__/`, a coverage file, a log). Never add such a file to a commit to empty the list, and never edit an ignore file for it unless the item asks. The build is served and graded from this worktree, and the check that follows your build refuses one that is not clean.
6. Write `{{ITEM_DIR}}/build-{{N}}.md` as described below, then create the empty file `{{ITEM_DIR}}/build-{{N}}.done`, then end your turn. Start nothing after that.

## Do not

- Do not merge, approve, close or force-push the pull request, and do not enable auto-merge. Merging is not yours.
- Do not write outside your worktree, or outside the paths the item says it may write. A `.playwright-mcp/` directory in the worktree belongs to the browser tool: never add it to a commit. A change needed anywhere else goes into your report as a proposed item, and the rest of the work continues.
- Do not work on another item, fix a nearby bug, or refactor in passing. Report it as a proposed item.
- Do not put AI attribution anywhere: no `Co-authored-by` line naming a tool, no "Generated with", no "Made with", in any commit, the pull request, or a file.
- Do not kill a process by name or pattern (`pkill`, `killall`, `kill $(pgrep …)`). Stop only what you started, by the PID you recorded when you started it. A slow command on a busy machine is not a hung one.
- Do not leave anything running. Stop every server you start before you finish, and check the port is free.
- Do not wait on a timer, and do not put work in the background and end your turn: a turn that ends before the done file exists has finished nothing. Run long commands in the foreground. To wait for something, poll it in a bounded foreground loop (`until <check>; do sleep 2; done`).
- Do not install anything on this machine (a browser, a global package, a tool). If something you need is missing, say so in your report.
- Do not print an environment (`env`, `printenv`, `docker inspect`, a `.env` file) or echo a token, even as evidence.
- Do not edit `CICD.yml`.
- Do not search outside the worktree and the item directory (`find /`, `find ~`, `mdfind`, `locate`).
- Do not add a human approval step, a checkpoint, or a question to be answered later.

## Write: `{{ITEM_DIR}}/build-{{N}}.md`

Plain facts, no argument for passing. It must contain these two lines exactly once each, in this shape, because a script reads them:

```
**Serve.** `<a command that starts the build on port {{PORT}} and keeps running until killed>`
**Ready.** URL `<a URL that answers once the build is up>`
```

When nothing is served, write instead:

```
**Serve.** `true`
**Ready.** COMMAND `<a command that exits 0 exactly when the change is present>`
```

Then:

- the pull request URL and the head commit (`git rev-parse HEAD`);
- the test and lint commands you ran with their pass and skip counts;
- what the evaluator needs in order to run the build that is not obvious (a seed step, a fixture, a login);
- `## Proposed items`: anything you noticed and did not fix, one line each, or "None";
- `## Disputed criterion` (rework only, and only if it applies): see the rework notes.
