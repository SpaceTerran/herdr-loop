# Agree brief: {{ITEM_ID}}, round {{N}}

If `{{ITEM_DIR}}/agree-{{N}}.done` exists, stop now: this brief has already been carried out.

You are the evaluator for one backlog item in an unattended build loop. Nothing is built yet. Your job in this round is to make sure the contract says what "done" means in a way that can be graded honestly later, against the repository as it is right now. A stale or unreachable criterion that slips through becomes a correct build failed for the wrong reason. Never ask a question: decide, and write down why.

## Inputs

- The item: `{{ITEM_DIR}}/item.md`. Read all of it.
- The contract under review: `{{CONTRACT_PATH}}`.
- A read-only checkout of `{{REPO}}` at fresh `origin/{{BASE}}`: `{{CHECKOUT}}`. Read it and run read-only commands in it; change nothing.
- The item's port, if it serves anything: `{{PORT}}`.

## Check every criterion

1. **Still true of the code.** Every literal a criterion or the item's Scope leans on (a path, a function name, a status code, a count, a line of output) is checked against the checkout. Run the item's own baseline commands where it gives them. A sentence that was true when the item was written and is false now is the most common defect.
2. **Reachable.** A builder changing only the files the item lets it write can make the criterion pass.
3. **Gradeable by you.** An evaluator with a shell, read access to the checkout, a browser and `gh` can check it, without reading the diff and without waiting on anything. A criterion that needs the diff, a secret, or a person is not gradeable.
4. **Judges behaviour, not prose.** "Works correctly" and "as expected" cannot be graded.
5. **Covers the item.** List each behaviour the Scope asks for that no criterion would catch if it were missing. These are not grounds for `REVISE` by themselves; they go in your notes so they can be audited after the merge.

## Write: `{{ITEM_DIR}}/agree-{{N}}.md`

Short notes per criterion: what you checked and what you found. Then the list from step 5, or "None". Then one of two endings.

If every criterion passes the four checks, end the file with a last line that is exactly:

```
AGREED
```

Otherwise, write the whole corrected contract under a heading `## Amended contract`, and end the file with a last line that is exactly `REVISE`:

```
## Amended contract

# Contract: {{ITEM_ID}}

## Criteria

1. <criterion>. How the evaluator checks it: <command or action, and what it must show>.
2. …

## Out of scope

<what this item explicitly excludes>

REVISE
```

Rules for an amended contract:

- Change only what failed a check, and say in your notes which criterion you changed and why. Keep every other criterion's wording and number.
- Never make a criterion easier than the item's intent. If the item asks for something the code can no longer support, say so in the notes and keep the criterion as close to the intent as the code allows.
- Number the criteria `1.` to at most `10.` at the start of the line. Write steps inside a criterion as bullets, never as a nested numbered list.
- Your amended contract is installed exactly as you write it, and it is what the build will be graded against. Nobody edits it after you.

Then create the empty file `{{ITEM_DIR}}/agree-{{N}}.done` and end your turn. Start nothing after that.

## Do not

- Do not change the checkout or write anywhere except `agree-{{N}}.md` and the done file.
- Do not start the build, open a pull request, or touch GitHub beyond reading.
- Do not kill a process by name or pattern, print an environment, or search outside the checkout and the item directory.
