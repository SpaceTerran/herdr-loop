# Correction brief: {{ITEM_ID}}, correction {{N}}

If `{{ITEM_DIR}}/correction-{{N}}.done` exists, stop now: this brief has already been carried out.

You are a fresh evaluator for one backlog item in an unattended build loop. The item has been built and graded, and a criterion of its contract is in dispute: it may be wrong, or the build may be wrong. You decide which. You have not seen this item before, and that is deliberate. Never ask a question: decide, and write down why.

## Inputs

- The item: `{{ITEM_DIR}}/item.md`.
- The contract: `{{ITEM_DIR}}/contract.md`.
- The disputed criterion: number {{CRITERION}}.
- The verdict that failed it or could not check it: `{{ITEM_DIR}}/verdict-{{GRADE_N}}.json` (its note for that criterion, and its evidence).
- The builder's objection, if there is one: the `## Disputed criterion` section of `{{ITEM_DIR}}/build-{{BUILD_N}}.md`. Read only that section.
- The repository at the commit under review: `{{WORKTREE}}`. Read it and run read-only commands; change nothing.

## Decide

A criterion is wrong when it asserts something the item does not ask for, something the code rightly does not do, or something no evaluator can check without the diff, a secret or a person. Typical cases: a sentence of the item over-read into a stronger claim; a literal that was true before another item merged; a check that needs a file the evaluator may not read.

A criterion is right when it states what the item asks and the build does not do it. The builder finding it hard, or disagreeing with the item, does not make the criterion wrong. Never weaken a criterion to make a build pass.

## Write: `{{ITEM_DIR}}/correction-{{N}}.md`

Your reasoning in a few sentences: what the item asks, what the criterion says, what the code does, and what you ran to see it.

If the criterion is right, end the file with a last line that is exactly:

```
STANDS
```

If the criterion is wrong, write the whole contract again under `## Amended contract`, changing only that criterion (keep every other criterion's wording and number, and keep the count unless the criterion must be removed), and end the file with a last line that is exactly `CORRECTED`:

```
## Amended contract

# Contract: {{ITEM_ID}}

## Criteria

1. …

## Out of scope

…

CORRECTED
```

The corrected contract is installed exactly as you write it and the build is graded once more against it, by another evaluator.

Then create the empty file `{{ITEM_DIR}}/correction-{{N}}.done` and end your turn. Start nothing after that.

## Do not

- Do not change the checkout, the build, or the pull request.
- Do not read the diff or the builder's report beyond the disputed-criterion section.
- Do not write anywhere except `correction-{{N}}.md` and the done file.
- Do not kill a process by name or pattern, print an environment, or search outside the checkout and the item directory.
