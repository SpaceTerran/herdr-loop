## What is still to do

Your build needs more work before it can go on. `{{REWORK_NOTE}}` says what and why; read it before anything else.

- Do what it names and nothing else. Do not rewrite what was not in question.
- Commit and push to the same branch. The pull request stays the same one; update its self-check table by writing the whole body again (`gh api --method PATCH repos/{{REPO}}/pulls/<number> -F body=@<file>`; `gh pr edit` does not work with this loop's token).
- To bring the branch up to its base, merge `origin/{{BASE}}` into it; never rebase or force-push a branch that has been reviewed.
