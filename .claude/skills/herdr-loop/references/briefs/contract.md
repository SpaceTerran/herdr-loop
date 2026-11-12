# Contract brief: {{ITEM_ID}}, draft {{N}}

If `{{ITEM_DIR}}/contract-draft-{{N}}.done` exists, stop now: this brief has already been carried out.

You will build one backlog item in an unattended build loop. Before any code is written, you and a separate evaluator agree in writing on what "done" means. In this step you propose that contract. Write no code and make no commit. Never ask a question: decide, and write down why.

## Inputs

- The item: `{{ITEM_DIR}}/item.md`. Read all of it.
- A read-only checkout of `{{REPO}}` at fresh `origin/{{BASE}}`: `{{CHECKOUT}}`.
- The item's port, if it will serve anything: `{{PORT}}`.

## Write: `{{ITEM_DIR}}/contract-draft-{{N}}.md`

```
# Contract: {{ITEM_ID}}

## Criteria

1. <one observable result>. How the reviewer checks it: <command or action, and what it must show>.
2. …

## Out of scope

<what this item explicitly excludes>
```

- At most ten criteria, numbered `1.` onward at the start of the line. Steps inside a criterion are bullets, never a nested numbered list.
- Every criterion is something a reviewer can check by exercising the build: with a shell, the checkout, a browser and `gh`. It states behaviour: not prose to be judged, and not "the code contains X" unless the item asks for exactly that. Leave suites and required checks out of the criteria: a script holds the required checks green at the merge.
- Every literal you write (a path, a name, a count, a status code) is one you checked in the checkout or one the build will create. Say which.
- A builder changing only the files the item lets it write must be able to make every criterion pass.
- Cover what the item asks. Do not add work the item does not ask for, and do not leave out a behaviour because it is hard to check: find the check.
- Repository settings, secrets and anything outside the repository are never criteria.

Then create the empty file `{{ITEM_DIR}}/contract-draft-{{N}}.done` and end your turn. Start nothing after that.

## The machine

This machine is shared: write nothing but the draft and the done file, write no code and open no pull request yet, stop only what you started (by PID, never by name or pattern), and never print an environment or a token.

{{SHELL}}
