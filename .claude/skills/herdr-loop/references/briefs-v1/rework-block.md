## This is a rework

Your first build was graded and did not pass. The verdict is `{{ITEM_DIR}}/verdict-{{GRADE_N}}.json`: its failed and unchecked criteria and its findings are your fix list. Read it before anything else.

- Fix what the verdict names, and nothing else. Do not rewrite what passed.
- Commit and push to the same branch. The pull request stays the same one; update its self-check table by writing the whole body again (`gh api --method PATCH repos/{{REPO}}/pulls/<number> -F body=@<file>`; `gh pr edit` does not work with this loop's token).
- If you are convinced a failed criterion is wrong and your build is right, do not bend the code to satisfy it. Make no change for that criterion, and add a section `## Disputed criterion` to your report: the criterion's number, what the item actually asks, what the code does, and the command that shows it. A fresh evaluator will rule on it.
