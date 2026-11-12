# Briefs

One brief per worker, per attempt. `hl-spawn` fills a template from the item's record, the program's settings and the repository's facts, saves it as `<phase>-<n>.brief.md` in the item directory, and sends the worker one line pointing at it. It refuses to render a brief with a value missing and refuses to send one that still contains `{{`. To see or change a brief before it is sent, render it with `hl-spawn brief`, edit the file, then `hl-spawn start`.

| Template | Role | Engine | Result file | Ends with |
|---|---|---|---|---|
| `agree.md` | evaluator (premise check) | Claude | `agree-<n>.md` | `AGREED`, or `## Amended contract` then `REVISE` |
| `contract.md` (phase `contract-draft`) | builder (loose items only) | either | `contract-draft-<n>.md` | (reviewed with `agree.md`) |
| `build.md` | builder | either | `build-<n>.md` | Serve/Ready lines |
| `review.md` (phase `grade`, runs whose policy is `review: code`) | evaluator (reviewer) | Claude | `verdict-<n>.json` | `PASS`, `FAIL`, `INCONCLUSIVE`, with a fix list |
| `confirm.md` (phase `grade`, a review the script gives as a delta confirm, in runs whose policy is `confirm: delta`) | evaluator (confirms a fix) | Claude | `verdict-<n>.json` | `PASS`, `FAIL`, `INCONCLUSIVE`, with an answer for each entry of the failed review's fix list |
| `grade.md` (phase `grade`, runs whose policy is `review: blind`) | evaluator | Claude | `verdict-<n>.json` | `PASS`, `FAIL`, `INCONCLUSIVE` |
| `correction.md` | evaluator | Claude | `correction-<n>.md` | `STANDS`, or `## Amended contract` then `CORRECTED` |

Every brief ends the same way: write the result file, create the empty `<phase>-<n>.done` file, end the turn. The done file is the only completion signal the manager trusts.

A manager's note reaches a worker in one of two ways. A brief rendered before anything is served (premise check, contract draft, build, correction) is rendered with `hl-spawn brief`, the note is written below it, and `hl-spawn start` sends it. A brief rendered once the build is served (review, confirm, grade) carries the note in its own slot, from `--note-file` on `hl-flow review`.

## Two sets

`briefs: v2` in `program.json` is this directory. `briefs: v1` is `../briefs-v1/`: the kit's first briefs, kept word for word (a test pins their hashes) so the two sets can be compared on the same work. A `program.json` that does not say gets `v1`; the template sets `v2`. What the first set never had (`review.md`, `blocks.md`, the two newer rework blocks) is taken from this directory either way.

## What changed from the first set, and why

The first set told a worker how, in twelve "do not" lines for a builder and eleven for an evaluator. Two of the evaluator's were read as forbidding the suite a criterion asked for, and cost three grades in the first real program. This set says what is wanted and why, and leaves the how.

A rule left the brief only where a script now holds it. Each line below is either held, kept, or listed as unproven: a rare incident does not show in a dozen items.

| First set | Now |
|---|---|
| No AI attribution anywhere | One sentence. Held by the clone's `commit-msg` hook, `hl-merge --after-build` and the merge gate |
| Leave the worktree clean and at the pull request's head | One sentence. Held by `hl-merge --after-build` |
| Do not enable auto-merge, merge, approve or close | One sentence. Held by `hl-merge --after-build` and the gate |
| Do not edit `CICD.yml` | Out of the brief. Held by `hl-merge --after-build` **when the repository's spec lists the file under `protected`**; otherwise unproven. One item may be let through for one named file the manager exempted for it (`hl-items exempt`, with a reason, in a run whose spec named it `exemptable`); `hl-merge` reads that again at the merge, and the builder's and reviewer's briefs say so |
| Do not write outside the item's paths; report, do not fix | Kept, with the reason (another stream shares the repository) |
| Do not kill by name or pattern; do not install on the machine; do not print an environment; do not search outside the worktree | Kept, under "The machine", with the reason (the host is shared) |
| Do not leave anything running | Kept |
| Do not wait on a timer, and do not put work in the background and end your turn | Changed. A worker with sub-agents may leave a command running while it works; the done file is written only when everything it started has finished. `hl-wait` no longer calls such a worker stopped. A worker without sub-agents is still told to run long commands in the foreground. **Unproven on the host** until the canary's sub-agent trials |
| (not in the first set) A command that never returns | One block, `shell`, in every brief, with the reason: nobody answers a prompt, so the attempt is spent. The example of a "bounded" loop the briefs gave, `until <check>; do sleep 2; done`, was not bounded, and hung a build when its server never started; the block gives one that counts its tries. **No script holds it**: `hl-wait` only reports the worker quiet |
| (not in the first set) Background jobs end without the worker | One bullet in `shell`: start each under `timeout`, or trap it in the shell that starts it, and check that none is left before the done file. A builder's four CPU loops, meant to be stopped by a `kill` that never ran, held four cores for nine hours (a help-desk catalog program, recorded among its decisions). Held by `hl-spawn close`, which kills what the worker's shells left running once its pane is closed (`hl/reap.py`) |
| Do not add a human approval step or a question to be answered later | Out of the brief ("nobody answers a question" stays). **Unproven** |
| Evaluator: do not read the diff or the builder's report | Reversed for a review: it reads the code, and takes nothing the builder says as shown. The blind grade keeps it |
| Evaluator: do not install anything; write nowhere but the evidence directory | A review runs what it needs in the checkout and leaves it as it found it, putting back a tracked file one of its commands rewrote. Held by `hl-flow reviewed`, which reads the checkout against its state at the review's start, and by the merge gate. The blind grade says where to run a suite a criterion names, and the same of the checkout |
| Evaluator: do not search outside the checkout; no attribution | Out of the review brief. **Unproven** (a reviewer commits nothing) |

## Blocks

`blocks.md` holds paragraphs that are put into a brief in place of a placeholder, each from its `### block: <name>` line to the next:

| Placeholder | Blocks | Chosen from |
|---|---|---|
| `{{LOOK}}` in `build.md` | `look-builder`, `look-builder-none`, `no-look-builder`, `look-builder-unknown` | the item's route and floor, and whether the worker has a browser (a Cursor builder has none) |
| `{{LOOK}}` in `review.md` | `look-review`, `no-look-review`, `look-unknown-review` | the item's route and floor |
| `{{LOOK}}` in `confirm.md` | `look-confirm` (with `{{LOOK_PAGES}}`), `no-look-confirm` | whether the failed review recorded looks |
| `{{REFERENCE}}` in `look-review` and `look-confirm` | `reference` (with `{{REFERENCE_ROOT}}`, `{{REFERENCE_URL}}`), or nothing | whether `hl-serve` is serving the repository's `reference` directory beside the build (`serve-<n>.json`) |
| `{{TESTING}}` | `testing-touched`, `testing-full`; `testing-train` for a run on a train | `prepush` in `program.json`; the run's policy |
| `{{HELPERS}}` | `helpers`, `no-helpers` | `subagents` in `program.json`, and the engine |
| `{{SETUP}}` | `setup-ready` | the item's `setup` field (`ready` when `hl-flow prepare` ran the repository's setup command) |
| `{{PROTECTED}}` | `protected` | the repository's `protected` paths, and any of them the manager exempted for this item (`hl-items exempt`) |
| `{{CHECKS}}` | `checks-known` (with `not-in-ci`), `checks-unknown`; `checks-train` for a run on a train | the repository's `ci_covers` and `not_in_ci`; the run's policy |
| `{{CHECKS}}` in `confirm.md` | `checks-confirm`; `checks-confirm-train` for a run on a train | the run's policy |
| `{{SHELL}}` in every brief of this set | `shell` | always: commands that never return (a read from stdin, an unbounded wait, a search from `/`, BSD `sed -i`), each of which stalled a worker in a help-desk feature program until the manager killed it; and background jobs, which must end without the worker |

`{{REPO_FACTS}}` is the repository's `facts` list from `program.json`, under a heading, or nothing. `{{DIFF_FACTS}}` is what `hl/review.py` reads from git for the pull request.

## Which contract a premise check reviews

- **Well-authored item:** install the Accept list first (`hl-items contract --from-accept`), then `{{CONTRACT_PATH}}` is `contract.md`.
- **Loose item:** run phase `contract-draft` first with a builder, then start the premise check with `--set CONTRACT_PATH=<item dir>/contract-draft-<n>.md`. `hl-spawn` names the drafter's session to `hl-items` by itself, so the drafter can never also review.

Either way, `AGREED` leaves or installs that text, and an evaluator's amended contract binds in the round it is written. In a run whose policy is `premise: routed`, a premise check runs only where the item's route or floor asks for one; elsewhere the builder plans for itself and may dispute a criterion in its report.

## A build after the first

`{{REWORK_BLOCK}}` is empty for a first build. For a later one it is one or more of: `rework-block.md` (the latest verdict of the build did not pass), `rework-checks-block.md` (`hl-wait --checks` wrote `checks-<build>.md`: required checks finished red), `rework-note-block.md` (the manager wrote `rework-<n>.md` in the item directory: the branch conflicts with its base, or the build was sent back twice). A further build with none of the three is refused.

## A delta confirm

In a run whose policy is `confirm: delta`, `hl-spawn` asks `hl/confirm.py` whether the review about to start is a full review or a delta confirm, and briefs it from `review.md` or `confirm.md`. A confirm's reviewer is a fresh session that did not write the code or the failed review. Its brief is the failed review's fix list, numbered as it must be answered, the two commits the fix lies between, and what the fix touches. It answers each entry, says whether the fix breaks anything, and does not go through the criteria again.

Beside a confirm's brief `hl-spawn` writes `grade-<n>.confirm.json`, the plan the brief was rendered from. `hl-spawn start` renders the brief again every time and sends a file that is already there only if it begins with exactly that text; what follows is sent too and counted (`brief_notes`), and the script cannot tell whose lines they are. The plan is a record of the decision, and nothing reads it back. `--full` on `hl-spawn brief`, `hl-spawn start` or `hl-flow review` asks for a full review; nothing asks for a delta.

Two things the brief asks for are also held by a script: `hl-verdict` refuses a verdict that does not answer every entry, and the merge gate reads the reviewer's transcript for each file's part of the fix, as this fix's own diff, in one result of its own commands, and for a look at each page the failed review looked at. One thing it asks for no script can hold: reading the fix against what the failed review passed.

## A correction

`{{DISPUTE}}` says where the dispute is read from: the verdict that failed the criterion or could not check it, the `## Disputed criterion` section of the latest build report, or both. A builder's dispute alone is enough in a `premise: routed` run, before any review. Pass the criterion with `--set CRITERION=<number>`.

## Where each value comes from

| Placeholder | Source |
|---|---|
| `{{ITEM_ID}}`, `{{RUN_ID}}`, `{{N}}`, `{{ITEM_DIR}}`, `{{REPO}}`, `{{EVIDENCE_DIR}}`, `{{BUILD_N}}`, `{{GRADE_N}}` | the run directory |
| `{{BASE}}` | where the item's pull request goes: the base in `program.json`, or the repository's train |
| `{{DEPLOY_BASE}}` | the base in `program.json`, where a train's batches are promoted |
| `{{TRAIN_RED}}`, `{{RED_FILE}}` | on a train: the `train-red` block while the latest tested batch is red, and the file that says what fails |
| `{{BRANCH}}`, `{{WORKTREE}}`, `{{CHECKOUT}}`, `{{PORT}}` | item fields recorded at Prepare. `{{CHECKOUT}}` falls back to the worktree. `{{PORT}}` reads "none (this item serves nothing)" when none is recorded |
| `{{PR_URL}}`, `{{PR_NUMBER}}`, `{{HEAD_SHA}}` | item fields `hl-merge --after-build` records |
| `{{HOW_TO_RUN}}` | the Serve and Ready lines of the latest build report |
| `{{READY_SUMMARY}}` | the sentence `hl-serve start` records |
| `{{CONTRACT_PATH}}` | `contract.md`, unless `--set` points at a draft |
| `{{CRITERION}}` | `--set CRITERION=<number>` |
| `{{FIRST_N}}`, `{{FROM_SHA}}`, `{{FIX_LIST}}`, `{{LOOK_PAGES}}` | for a delta confirm: the failed review's number, the commit it was of, its fix list and the pages it looked at, from the attempt log and `verdict-<n>.json` |
| `{{DELTA_FACTS}}` | for a delta confirm: what `hl/review.py` reads from git for the change between `{{FROM_SHA}}` and `{{HEAD_SHA}}` |
| `{{MANAGER_NOTE}}` | `--note-file <file>` on `hl-flow review`, `hl-spawn brief` or `hl-spawn start`: the file's text under the heading "A note from the manager"; empty without one. Only `review.md`, `confirm.md` and this set's `grade.md` have the slot. They say what the served build answered, so they are rendered in the command that serves it, and a note written below an earlier rendering never matched (the first item of a help-desk feature program). Every other brief is rendered before anything is served and takes notes below it; `--note-file` is refused for it |
