# Correction brief: {{ITEM_ID}}, correction {{N}}

If `{{ITEM_DIR}}/correction-{{N}}.done` exists, stop now: this brief has already been carried out.

You are a fresh evaluator for one backlog item in an unattended build loop. A criterion of the item's contract is in dispute: either the criterion is wrong or the build is. You decide which. You have not seen this item before, on purpose. Nobody answers a question: decide, and write down why.

## What you have

- The item: `{{ITEM_DIR}}/item.md`.
- The contract: `{{ITEM_DIR}}/contract.md`.
- The dispute: {{DISPUTE}}
- The repository at the commit under review: `{{WORKTREE}}`. Read it and run read-only commands in it; change nothing in it.

## Decide

A criterion is wrong when it asserts something the item does not ask for, something the code rightly does not do, or something no reviewer can check without a secret or a person. Typical cases: a sentence of the item read into a stronger claim; a literal that was true before another item merged; a value the item pins that an earlier check replaced with one of its own making.

A criterion is right when it states what the item asks and the build does not do it. The builder finding it hard, or disagreeing with the item, does not make the criterion wrong. Never weaken a criterion to make a build pass. A value the item pins is the item's: it is wrong only when the checkout contradicts it.

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

The corrected contract is installed exactly as you write it, and the build is reviewed against it by another evaluator.

Then create the empty file `{{ITEM_DIR}}/correction-{{N}}.done` and end your turn. The done file is the only thing that says you are finished.

## The machine

This machine is shared: write nothing but your result file and the done file, change neither the checkout nor the pull request, stop only what you started (by PID, never by name or pattern), and never print an environment or a token.

{{SHELL}}
