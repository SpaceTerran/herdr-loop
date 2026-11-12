## This is a rework

Your build was reviewed and did not pass. The verdict is `{{ITEM_DIR}}/verdict-{{GRADE_N}}.json`. Read it before anything else: its fix list, where it has one, is what you must change, and its failed and unchecked criteria say what was not shown.

- Fix what the fix list and the failed criteria name, and nothing else. Do not rewrite what passed. The verdict's `findings` are kept for follow-up and are not yours to act on.
- Commit and push to the same branch. The pull request stays the same one; update its self-check table by writing the whole body again (`gh api --method PATCH repos/{{REPO}}/pulls/<number> -F body=@<file>`; `gh pr edit` does not work with this loop's token).
- If you are convinced a failed criterion is wrong and your build is right, do not bend the code to satisfy it. Change nothing for that criterion, and say so in your report under `## Disputed criterion`, one line starting `Criterion <n>:` with what the item asks, what the code does, and the command that shows it. A fresh evaluator rules on it.
