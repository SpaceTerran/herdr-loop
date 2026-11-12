# Build brief: {{ITEM_ID}}, build {{N}}

If `{{ITEM_DIR}}/build-{{N}}.done` exists, stop now: this brief has already been carried out.

You are building one backlog item in an unattended loop, as one real pull request. A separate reviewer then reads your code and exercises the change; a pass merges it with no person in between, and a fail comes back to you as a fix list. Nobody answers a question: where the item leaves a choice open, make it and say what you chose.

## What you have

- Repository `{{REPO}}`, base branch `{{BASE}}`. Your worktree is `{{WORKTREE}}`, on branch `{{BRANCH}}`, cut from fresh `origin/{{BASE}}`. Work only there.
- The item: `{{ITEM_DIR}}/item.md`. The contract your work is reviewed against: `{{ITEM_DIR}}/contract.md`.
- Port: `{{PORT}}`. Serve on that port and no other; another stream of this program may be serving on its own.
- Use these values as written wherever one is called for: item `{{ITEM_ID}}`, run `{{RUN_ID}}`, branch `{{BRANCH}}`.
- If the branch already has commits, or a pull request from it is already open, an earlier builder of this item was stopped before it finished. Read what it left (`git log origin/{{BASE}}..HEAD`, `gh pr list --repo {{REPO}} --head {{BRANCH}} --state open`), treat it as your own unfinished work, and use the pull request that is open.
- A path the item names outside your worktree is where its author read something, not somewhere to go looking. Check the fact in your worktree, or take it as written.

{{SETUP}}

{{REPO_FACTS}}

{{REWORK_BLOCK}}

## Plan before you build

Read the item and the contract, then check each criterion against your worktree before you write code: is what it leans on still true of the code, and can a change inside the paths the item lets you write make it pass? The plan is yours and need not be written down.

If a criterion is wrong (it asserts something the item does not ask for, something the checkout contradicts, or something no change inside your paths can reach), do not bend the code to it. Build what the item asks, change nothing for that criterion alone, and say so in your report under `## Disputed criterion`, one line per criterion, starting `Criterion <n>:` and giving what the item asks, what the checkout shows, and the command that shows it. A fresh evaluator rules on it before your work is reviewed. A value the item pins (a hash, a count, a size) is wrong only when the checkout contradicts it, never because your own first attempt produced another.

## Build

- Build every criterion, and what the item's Scope asks even where no criterion proves it. Where a criterion states a literal (a file name, a title, a count), match it literally.
- Stay inside the paths the item says it may write, with the one exception in the next point: another stream may be working in this repository at the same time. Something that needs changing elsewhere, a nearby bug, a refactor you would like, a test you would add beyond what the item asks: put it under `## Proposed items` in your report and leave it.
- One kind of file is part of your change wherever it lives: a test that already exists and asserts the behaviour this item changes (the old message, the old status, the old shape of an answer), or that holds a list the item adds to or takes from (an inventory of routes, tables, tasks or files). Such a test fails the moment your change is right, and the repository's checks then fail on your pull request for a file you never opened. Before you push, search the tests for what you changed (`grep -rn` for the function, the route, the message or the name) and run the ones you find. Update each to assert the new behaviour, in this pull request, and name them in your report. Keep what the test guards: change the expectation the item changes and nothing else. Never skip or loosen one, and delete one only when the item removes the very thing it tests. A Scope sentence or a list of paths that leaves such a test out does not stop you: this brief allows the edit, and your report names it. Where a numbered criterion cannot pass once the test is updated (it pins the diff to one file, say), that criterion is wrong in the sense of "Plan before you build": update the test, and dispute the criterion there as described. This exception is for tests only, and never for a path the repository protects.

{{TESTING}}

{{LOOK}}

{{HELPERS}}

## Hand it over

1. Commit with explicit paths (`git add <paths>`) and a plain message. This clone refuses a commit whose message names a tool, and so does the merge; some tools add such a line by habit, so read `git log --format=%B origin/{{BASE}}..HEAD` before you push.
2. Push the branch and open one pull request against `{{BASE}}`. Its body is the contract file word for word, then the two Serve/Ready lines below, then a table with one row per criterion: what you ran and what it printed. Write the body to a file under `.scratch/` in the worktree, which this clone ignores, and pass it with `--body-file`. Read it back (`gh pr view <number> --json body --jq .body`); if your tool added a closing line of its own, write the body again without it (`gh api --method PATCH repos/{{REPO}}/pulls/<number> -F body=@<file>`; `gh pr edit` does not work with this loop's token).
3. Leave the worktree as the pull request has it: `HEAD` is the commit you pushed and `git status --porcelain` prints nothing. Scratch output belongs under `.scratch/`. The build is served and reviewed from this worktree.
4. Stop everything you started and check your port is free.
5. Write `{{ITEM_DIR}}/build-{{N}}.md` (below), then create the empty file `{{ITEM_DIR}}/build-{{N}}.done`, then end your turn. The done file is the only thing that says you are finished: a turn that ends without it has finished nothing, whatever is still running.

A script checks the build the moment the done file appears, and sends it back to you if the pull request is missing, a draft, set to merge itself, or lacks the contract; if a commit or the body carries a tool's attribution; if the worktree is not clean and at the pull request's head{{PROTECTED}}. Merging, approving and closing the pull request are not yours: a script merges on the reviewer's pass.

## The machine

This machine is shared with other loops and a live assistant, so:

- Stop only what you started, by the PID you recorded when you started it; never by name or pattern. A slow command on a busy machine is not a hung one.
- Install nothing outside your worktree. A virtualenv or `node_modules` inside it is part of the work; a global package or a browser is not. If something you need is missing from the machine, say so in your report.
- Never print an environment or a token, even as evidence: what a pane shows is kept and read.
- Search inside your worktree and the item directory, not the home directory or the disk.

{{SHELL}}

## The report: `{{ITEM_DIR}}/build-{{N}}.md`

Plain facts, no argument for passing. It contains these two lines exactly once each, in this shape, because a script reads them:

```
**Serve.** `<the command that starts the real server on port {{PORT}} and keeps running until killed>`
**Ready.** URL `<a URL that answers once the build is up>`
```

For an item routed with a look, the Serve line must start the real server: an empty line, `true`, `:` or any other no-op is sent back, because a reviewer cannot look at a build that is not running. When nothing is served (an item with no look), write instead:

```
**Serve.** `true`
**Ready.** COMMAND `<a command that exits 0 exactly when the change is present>`
```

Then:

- the pull request URL and the head commit (`git rev-parse HEAD`);
- for an item with a look: each value the item pins, with what you measured in the served pages;
- the test and lint commands you ran, with their pass and skip counts; a skip you cannot explain is a failure;
- what a reviewer needs in order to run the build that is not obvious (a seed step, a fixture, a login);
- `## Proposed items`: what you noticed and left alone, one line each, or "None";
- `## Disputed criterion`: only if you dispute one, as described above.
