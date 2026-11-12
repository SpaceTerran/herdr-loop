# Blocks

Paragraphs that `hl-spawn` puts into a brief in place of a placeholder, chosen from the item's route, the run's policy and the repository's facts in `program.json`. Each block runs from its `### block:` line to the next one. A block may use any value the brief itself may use.

### block: look-review

**This item changes something a person looks at.** Open every page it touches with the browser tool on the running build and compare it with the reference the item names, in every mode the item names (dark and light, for one), at a 1280×800 viewport. Compare what is measurable as measured values (a computed color, a size, a spacing, the font), and the page as a whole by eye against the reference. A value a few pixels or one shade off in one mode is a defect. Keep one screenshot for each page and mode and record each as a look. A page you could not open is `not_checked`, never `pass`. A look is a page the browser tool opened: the merge counts the browser tool's navigations to the served build (`browser_navigate`), so a page a Playwright script or any other program opened from the shell is not a look, whatever its screenshot shows. Such a script exercises the build; it does not look at it.

{{REFERENCE}}

### block: no-look-review

**This item was routed as changing nothing a person looks at.** Check that against the diff before you rely on it. If the change does alter what a page shows, set `look_needed` to `true` and look: open the pages it affects with the browser tool on the running build, in each mode they have, and record each look. If it alters a page and nothing is served for you to open, set `look_needed` to `true`, record no look, and say which page: the verdict is then `INCONCLUSIVE`, and the item is routed again with a look. A look is a page the browser tool opened: the merge counts the browser tool's navigations to the served build (`browser_navigate`), so a page a Playwright script or any other program opened from the shell is not a look, whatever its screenshot shows. Such a script exercises the build; it does not look at it.

### block: checks-known

**Required checks are not yours to run or to wait for.** A script holds every required check green on the commit that merges, whatever your verdict says, and they may still be running while you review. In this repository they run:

{{CI_COVERS}}

Do not reinstall, run the full suite or rebuild: CI repeats them. Read the code, use the served build, and run a targeted test only where a criterion needs a result CI does not give. A criterion that only restates one of them, or that only asks for the required checks to pass, is `checks`: the merge enforces it. A criterion that asks for behaviour is yours to exercise even if a check also covers it.

{{NOT_IN_CI}}

### block: checks-unknown

**Suites.** Nothing is recorded about what this repository's required checks run, so where a criterion names a suite or a check, run it or read its state yourself (`gh pr checks {{PR_NUMBER}} --repo {{REPO}}` shows the checks; if one a criterion needs is unfinished, mark that criterion `not_checked`). A script holds the required checks green on the commit that merges, whatever your verdict says.

### block: not-in-ci

No required check runs these, so nothing but this review stands between them and the merge. Where the diff touches what one of them covers, run the part it touches:

{{NOT_IN_CI_LIST}}

### block: look-unknown-review

**Decide first whether this change alters anything a person looks at.** If it does, set `look_needed` to `true`, open every page it affects with the browser tool on the running build, in every mode the item names or the page has, at a 1280×800 viewport, and compare each with the reference the item names: measurable things as measured values, and the page as a whole by eye. A value a few pixels or one shade off in one mode is a defect. Keep one screenshot for each page and mode and record each as a look. A look is a page the browser tool opened: the merge counts the browser tool's navigations to the served build (`browser_navigate`), so a page a Playwright script or any other program opened from the shell is not a look, whatever its screenshot shows. Such a script exercises the build; it does not look at it.
### block: testing-touched

- **Test what you touch while you work**, and before you push run the tests and linters that cover what you changed (the tests related to the files you touched, not the full suites), in the foreground or with a `runner`. The repository's required checks run its full suites once, on your pull request, so a full local run of them is not asked of you; what its facts list as `ci_covers` is run there, and what they list as `not_in_ci` is yours where you touch it; if a check comes out red, the failed runs come back to you to fix. Two kinds of check fail on what a change adds far more often than on a line it edits, so run them on what you touched even though CI runs them too: the scanners and linters the repository pins, at the version it pins (a security scanner such as checkov over the infrastructure you changed, a formatter's check), and any test that holds a list of files to the tree (an index in a README, a registry of scripts or of pages) when you added, renamed or removed such a file. "The tests related to the files you touched" are not only the tests beside them: a test in another directory that asserts what you changed is related too, and is found by searching the tests for the name, route or message you changed, not by where it sits. On one program 20 of 21 red test runs failed in a test file the pull request had not touched, and those items took a median 98 minutes against 36 for the rest. Send long output through `tee` to a file under `.scratch/` and read that file; do not run a slow command twice to see its output differently.

### block: testing-full

- **Test what you touch while you work, and run the repository's tests and linters once in full, the way its CI does, before you push.** A red check on your pull request costs a round of CI. Send long output through `tee` to a file under `.scratch/` and read that file; do not run a slow command twice to see its output differently.

### block: look-builder

- **This item changes something a person looks at.** Open it in the browser and compare it with the reference the item names, in every mode the item names, before you call it built: the measurable things as measured values, and the page as a whole by eye. The reviewer does the same, to the pixel. **Serve the pages and check in the browser every value the item or its reference pins** (colours, sizes, padding and gaps, button variants, labels), using computed styles where the value is a style, and list each one in your report with what you measured. A pinned value you did not measure is the usual reason a review fails. A `.playwright-mcp/` directory in the worktree belongs to the browser tool and is never committed.

### block: look-builder-none

- **This item changes something a person looks at, and this session has no browser.** Build to the reference's measured values, check what a shell can check (the served markup, the stylesheet, the computed tokens), and say in your report that you did not look. The reviewer looks, to the pixel.

### block: no-look-builder

- This item was routed as changing nothing a person looks at. If you find that it does (a value, a template, a stylesheet or a message that a page renders), say so in the first line of your report.

### block: helpers

- **How you get there is yours.** You have two sub-agents: `scout` reads and explores for you and reports back in a few lines, which keeps long reads out of your own context; `runner` runs one command to completion and reports its result, and several can run side by side. They change nothing; the building is yours. A command you leave running in the background is fine while you work on something else, but the done file is written only when everything you started has finished and you have read its result.

### block: no-helpers

- **How you get there is yours.** Run long commands in the foreground and wait for them. To wait for something, poll it in a foreground loop that counts its tries (see "Commands that never return").

### block: setup-ready

- **Your worktree is ready.** `{{SETUP_COMMAND}}` was run in it before you started, so its dependencies are installed. Do not reinstall (`npm ci`, `pip install` and the like) unless your change touches a package or lock file; if it does, run that command again and say so in your report.

### block: protected

; or if the pull request changes a path no item may change ({{PROTECTED_LIST}})

### block: look-builder-unknown

- If this item changes anything a person looks at, open it in a browser, where this session has one, and compare it with the reference the item names before you call it built.

### block: testing-train

- **Test what you touch while you work**, and before you push run the tests and linters that cover what you changed, in the foreground or with a `runner`. Your pull request goes into `{{BASE}}`, the branch this program's items are gathered on, and it merges there on its review: the repository's full checks are not held on it, whether or not any of them starts. They are run later, on several items together, before any of them reaches `{{DEPLOY_BASE}}`. {{ITEM_CHECKS}} So before your change merges, your own run and the review are what there is; a failure that only the full checks find comes back as a repair after other work has been built on yours. Do not run the full suites yourself, and do not wait for them: never `gh pr checks --watch` on this pull request. Only the quick checks above matter before you hand it over. Two kinds of check fail on what a change adds far more often than on a line it edits, so run them on what you touched although the full checks run them too: the scanners and linters the repository pins, at the version it pins (a security scanner such as checkov over the infrastructure you changed, a formatter's check), and any test that holds a list of files to the tree (an index in a README, a registry of scripts or of pages) when you added, renamed or removed such a file. Here a failure of either is found only on a batch, and costs a repair after other items were built on yours. Send long output through `tee` to a file under `.scratch/` and read that file. {{TRAIN_RED}}

### block: train-red

The full checks failed on that branch when it was last tested, and a repair is under way: `{{RED_FILE}}` says what failed. A test that fails for that reason is not yours to fix and not a finding; name it in one line of your report. One that fails because of this change is.

### block: checks-train

**The repository's full checks are not held on this pull request.** It merges into `{{BASE}}`, the branch this program's items are gathered on, on your review, whether or not any of them has run. They are run afterwards, on several items together, and what they run is:

{{CI_COVERS}}

{{ITEM_CHECKS}} So nothing but this review stands between this change and that branch, and other items will be built on it at once. Do not reinstall dependencies, run a full suite or rebuild. Run the tests that cover what the diff touches: the part of a suite that exercises the changed code and what calls it, not the whole suite. A criterion that only restates one of the full checks is `checks`: the batch holds it before anything reaches `{{DEPLOY_BASE}}`. A criterion that asks for behaviour is yours to exercise.

{{NOT_IN_CI}}

{{TRAIN_RED}}

### block: look-confirm

**A person looks at what this item changes.** Review {{FIRST_N}} looked at the pages below. Open each again with the browser tool on the running build, in the same mode, at a 1280×800 viewport, and compare it with the reference the item names: what is measurable as measured values, and the page as a whole by eye. A value a few pixels or one shade off in one mode is a defect. Open any other page the fix changes as well. Keep one screenshot for each page and mode and record each as a look: the merge checks that every page below has one. A look that differs is an entry in your fix list. A look is a page the browser tool opened: the merge counts the browser tool's navigations to the served build (`browser_navigate`), so a page a Playwright script or any other program opened from the shell is not a look, whatever its screenshot shows. Such a script exercises the build; it does not look at it.

{{REFERENCE}}

{{LOOK_PAGES}}

### block: no-look-confirm

**Review {{FIRST_N}} recorded no look at a page.** Check the fix against that. If the fix alters what a page shows, set `look_needed` to `true` and look: open the pages it affects with the browser tool on the running build, in each mode they have, and record each look. If it alters a page and nothing is served for you to open, set `look_needed` to `true`, record no look, and say which page: the verdict is then `INCONCLUSIVE`, and the next review is a full one.

### block: checks-confirm

**Required checks are not yours to run or to wait for.** A script holds every required check green on the commit that merges, whatever your verdict says, and they may still be running while you work. Do not reinstall, run a full suite or rebuild where `ci_covers` applies: CI repeats them. Run what shows an entry fixed, and the tests that cover what the fix touches.

### block: checks-confirm-train

**The repository's full checks are not held on this pull request.** It merges into `{{BASE}}`, the branch this program's items are gathered on, on your verdict, and the full checks run afterwards on several items together. {{ITEM_CHECKS}} Do not reinstall, run a full suite or rebuild where `ci_covers` applies: CI repeats them. So run what shows an entry fixed, and the tests that cover what the fix touches: the part of a suite that exercises the changed code and what calls it, not the whole suite.

{{TRAIN_RED}}

### block: shell

**Commands that never return.** Nobody watches your pane to answer a prompt or end a stuck command, so a command that waits forever ends this attempt with no result: the manager only sees that you went quiet, and the attempt is spent. Each of these has done that here:

- Never run a command that reads input with nothing to read: a bare `cat`, `cat > file` without a heredoc, `read`, `less`, an editor, a REPL. Write a file with the Write tool or a heredoc.
- Give every wait an end. A polling loop counts its tries (`for i in $(seq 60); do curl -fsS <url> >/dev/null && break; sleep 2; done`), never a bare `until <check>; do sleep 2; done`, which polls forever when the thing never starts. When the wait runs out, read why (the server's log, the command's output) instead of waiting again. `timeout <seconds> <command>` gives any one command an end.
- Whatever you start in the background (a load generator, a server, a helper, a watch loop) must end without you: start it under `timeout <seconds>`, or trap it in the shell that starts it (`pids=(); trap 'kill "${pids[@]}" 2>/dev/null' EXIT INT TERM HUP` first, `pids+=($!)` after each). A `kill` at the end of the command never runs if the shell dies first, and what it was to stop runs on without a parent: four CPU loops a builder left that way held four cores for nine hours. Check that none of it is still running before you write your done file; whatever is left when your pane is closed is killed.
- Never search from `/` or the home directory (`find /`, `find ~`, `mdfind`, `locate`): a mount there blocks the search for many minutes. What you need is in your checkout, the item directory, or a path this brief names; a file you wrote yourself is where you wrote it. If something is not where you expected, say so in your result.
- This machine is macOS, with BSD tools: `sed -i` takes a suffix argument (`sed -i '' 's/a/b/' file`), and without it the script is taken as the suffix and the command fails. Check that a step succeeded before you chain the next one on it with `&&`.

### block: reference

**The reference pages are served beside the build**: `{{REFERENCE_ROOT}}/` of this checkout, at {{REFERENCE_URL}} (a file `{{REFERENCE_ROOT}}/<path>` is at {{REFERENCE_URL}}<path>). The browser tool does not open `file://`, so open there, with the browser tool, the reference the item names, in the same mode and at the same viewport as the build, and compare the two pages rendered, not the build with the reference's source. Opening the reference is not a look at the build: record a look for each page of the build.
