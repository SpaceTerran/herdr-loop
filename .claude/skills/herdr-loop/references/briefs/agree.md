# Premise brief: {{ITEM_ID}}, round {{N}}

If `{{ITEM_DIR}}/agree-{{N}}.done` exists, stop now: this brief has already been carried out.

You are checking one backlog item's contract before anything is built, in an unattended build loop. The contract is what a reviewer will later hold the build to, against the repository as it is right now. A criterion that is stale or out of reach, left in, becomes a sound build failed for the wrong reason; a sound criterion changed here becomes the same thing. Nobody answers a question: decide, and write down why.

## What you have

- The item: `{{ITEM_DIR}}/item.md`. Read all of it.
- The contract under review: `{{CONTRACT_PATH}}`.
- A checkout of `{{REPO}}` at fresh `origin/{{BASE}}`: `{{CHECKOUT}}`. Read it and run read-only commands in it; change nothing in it.
- The item's port, if it serves anything: `{{PORT}}`.

{{REPO_FACTS}}

How the build will be reviewed, so you do not write its setup into a criterion: a script serves the build from the builder's worktree before the review starts, with its dependencies installed, and the reviewer drives it with a shell, a browser and `gh`, and can read the code. The reviewer starts nothing and installs nothing. Required checks are held green at the merge by a script.

## Check every criterion

1. **Still true of the code.** Every literal a criterion or the item's Scope leans on (a path, a name, a status code, a count, a line of output) is checked against the checkout. Run the item's own baseline commands where it gives them. A sentence that was true when the item was written and is false now is the most common defect.
2. **A value the item pins is the item's.** A hash, a count or a size the item states for something the build will produce is stale only when the checkout contradicts it. It is never stale because your own rebuild of the thing gave another value: the builder builds from the item, not from your attempt.
3. **Reachable.** A builder changing only the files the item lets it write can make the criterion pass. A criterion that the base branch already breaks, in files the item may not write, is not reachable.
4. **Reviewable.** The reviewer described above can check it without waiting on anything, a secret, or a person.
5. **Judges behaviour, not prose.** "Works correctly" and "as expected" cannot be checked.
6. **Covers the item.** List each behaviour the Scope asks for that no criterion would catch if it were missing. These are not grounds for `REVISE` by themselves; they go in your notes so they can be audited after the merge.

## Write: `{{ITEM_DIR}}/agree-{{N}}.md`

Short notes per criterion: what you checked and what you found. Then the list from step 6, or "None". Then one of two endings.

If every criterion passes, end the file with a last line that is exactly:

```
AGREED
```

Otherwise write the whole corrected contract under a heading `## Amended contract`, and end the file with a last line that is exactly `REVISE`:

```
## Amended contract

# Contract: {{ITEM_ID}}

## Criteria

1. <criterion>. How the reviewer checks it: <command or action, and what it must show>.
2. …

## Out of scope

<what this item explicitly excludes>

REVISE
```

- Change only what failed a check, and say in your notes which criterion you changed and why. Keep every other criterion's wording and number. Add no criterion the item does not ask for.
- Never make a criterion easier than the item's intent. If the item asks for something the code can no longer support, say so in the notes and keep the criterion as close to the intent as the code allows.
- Number the criteria `1.` to at most `10.` at the start of the line. Steps inside a criterion are bullets, never a nested numbered list.
- Your amended contract is installed exactly as you write it. Nobody edits it after you.

Then create the empty file `{{ITEM_DIR}}/agree-{{N}}.done` and end your turn. The done file is the only thing that says you are finished.

## The machine

This machine is shared: write nothing but your result file and the done file, stop only what you started (by PID, never by name or pattern), and never print an environment or a token. Start no build and open no pull request.

{{SHELL}}
