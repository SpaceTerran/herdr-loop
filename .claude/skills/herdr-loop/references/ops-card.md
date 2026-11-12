# Ops card: the manager's commands

The manager prompt says what to do; this card says how, and what each answer means. Where the two disagree about a command, this card wins. What is known about the host and about workers there is in `host-facts.md`; what to do when something goes wrong is in `troubles.md`.

`<kit>` is `<root>/.claude/skills/herdr-loop`, and every script is in `<kit>/scripts/`. Each prints one JSON object, and exits 2 with `{"refused": [...]}` when it will not do what was asked. A refusal is an answer: fix the cause, never the script.

The scripts that reach GitHub (`hl-flow`, `hl-merge`, `hl-train`, `hl-verify`, `hl-wait` with `--checks` or `--train`) run on the loop's own token and refuse without it: `git` and `gh` otherwise fall back to the `gh` keyring's token, which may be able to do far more (on the author's host it could administer the organization). They fetch that token themselves from the program's keeper (`hl-secrets`, under "Secrets" below), which reads each secret once through the program's secrets reader (`hl-read-secret`, or `secrets_reader` in `program.json`) and holds it in memory. That is so for a program whose `program.json` says `"secrets": "keeper"`, which staging writes; the last line under "Secrets" is for one that does not. No line of yours sets `GH_TOKEN`, and you do not read a secret by hand: a store may count every read (a 1Password service account allows 1,000 requests a day, and a manager that read the token on every shell call used them up in ten hours and stopped its program for fifteen), and what you run stays in your transcript. A `gh` call of your own gets the token the same way:

```
<kit>/scripts/hl-secrets --root <root> run -- gh pr view <n> --repo <owner/name> --json state
```

## The session

```
hl-claim --root <root> claim          # every session start; exit 2 if another live manager holds the program
hl-claim --root <root> pin            # once per program: record path and hash of claude, cursor-agent, gh, git, herdr
hl-claim --root <root> check          # every session start; accepts and records any binary that updated itself
hl-claim --root <root> release --note "hand-over at <n> tokens"
hl-claim --root <root> repin --name <name> --note "<why>"   # only to pin a different file by hand
hl-flow  --root <root> save --note "<what changed>"
hl-flow  --root <root> archive --dry-run [--out <root>/runs/gates/archive-sweep-<UTC>.md]   # what would move; no claim needed
hl-flow  --root <root> archive --swept <that file>   # move what the sweep listed and a sub-agent read
```

- Auto-updates are normal (Claude Code, Cursor, Homebrew's `gh`). `check`, and every script that uses a pinned binary, accepts a changed one on its own and records it in `pins.json`'s history with its version; its answer lists them under `updated`. Nothing to do, nothing to ask, no ledger entry. `check` refuses only a binary that is gone and stays gone for a minute: wait a few minutes and run it again (an installer may still be running); if it is still gone, see that the host has it installed, as for any other host fault.
- `save` writes `LEDGER-ITEMS.md` and `FOLLOW-UPS.md` beside the ledger, commits and pushes the ledger clone, and stamps the heartbeat. It answers `context_tokens`, and `hand_over: true` once that passes the program's `handover_tokens`. Exit 2 with `pushed: false` means the commit is local: read `push_error`, fix it, save again. It also answers `outside_prs_ready`, read after the heartbeat is stamped and never a reason for the save to fail: `adopt` (whether this program may merge one), and under `prs` each open pull request a steward labeled `rollout-ready` in the program's repositories, with `checks` (`green`, `pending: <names>`, `red: <names>`, `none required`), `behind`, `mergeable`, `merge_state` and `branch_allowed`; `errors` or `not_checked` when GitHub could not be read. What to do with them: "Pull requests from outside the program", below. And it answers `idle`, read from the run directories: under `streams`, each stream with no item in progress, with `since` (the last time an item of it changed status or a run of it was made), `minutes` and `pending` (items waiting in its runs); `runs_outside_streams` when a run with items still to work belongs to no stream of the program; `errors` when a run directory could not be read. A stream between items (a Verify waited on, a prepare running) is listed too. What to do with one listed for longer is in the manager prompt, "When a stream has nothing to run". Last, it answers `clocks`: under `runs`, each run with an item still pending, in progress or parked whose file's header says a wait; under `items`, each such item whose own text does; under `gates`, each gate of the ledger's Gates table whose last row is not passed, done or dropped and says one. Each entry has `kind` (`wait`: a next read, a soak, T+, a queue left to drain, until a date; `clock`: a length of time, looked for in gates only; `edge`: another run's end) and `says`, the first such words with a little of their line; `errors` when a run or the ledger could not be read. What to do with each is in the manager prompt, "Nothing waits to see whether it fixes itself". It answers `unstarted_files` too: `item_files`, the backlog files (`*-ITEMS.md`) beside the ledger, and under `files` each one that no run's `run.json` carries and that no line of the ledger names after `held:`, `dropped:` or `superseded:` (`superseded: OLD by NEW` ends OLD), nor any line of `LEDGER-ARCHIVE.md` after `dropped:` or `superseded:` (a hold lasts while its row is in the ledger); a voided run (`runs/.void-*`) carries nothing, and is not counted in `idle`, `clocks`, `ci_time` or the generated files either. What to do with one: "When a stream has nothing to run", step 2. And where `program.json` sets `ledger_archive_kb`, it answers `archive`: `ledger_kb`, `movable_kb` (what `hl-flow archive` would move now, from the ledger and `FOLLOW-UPS.md` together), `at_kb` and `due`; `errors` when the ledger or `FOLLOW-UPS-ARCHIVED.json` could not be read (the save then writes every follow-up into `FOLLOW-UPS.md`, never fewer). `FOLLOW-UPS.md` leaves out the reports an archive moved while each reads as it did then; one written again since is listed again.

- `archive --dry-run` answers `would-move` with `decisions` (`rows`, `numbers` as ranges, `kept`), `gates` (`rows` and the gates' names), `followups` (`reports`, `rows`) and `movable_kb`, and writes nothing beside the ledger; with `--out` it writes every row that would move to that file (the folder must exist) for the sub-agents that read it. `archive --swept <file>` needs the claim and that file: it moves only what the file lists, and answers `archived` with the same counts, `ledger_kb_after`, and `unswept` (`decisions`, `gates`, `followup_rows` that would have moved but were written, edited or finished after the dry run: they wait for the next archive), or `nothing-to-move`. Either answer, and the dry run's, carries `broken` when a row due to move is broken over two lines (`decisions` by number, `gates` by name), or a gate's last row reads more than one way or has fewer than three cells (`unclear_gates`, never taken as settled): it stays until it is mended (`references/troubles.md`). Exit 2 is a refusal: no sweep file, a file a dry run did not write, the program not yours, or `nothing was archived: …` (`references/troubles.md`). The next `save` commits what it wrote. When to run it: the manager prompt, "Keeping the ledger short".

## One item

```
hl-flow --root <root> --run-dir <run> prepare  --id <ID> [--port <n>|none]
hl-items --run-dir <run> route --id <ID> --premise builder|independent --look yes|no --reason "<why>"
hl-spawn --root <root> --run-dir <run> start --id <ID> --phase build --engine cursor|claude [--effort <e>] --why "<a line>"
hl-flow --root <root> --run-dir <run> built    --id <ID>
hl-flow --root <root> --run-dir <run> review   --id <ID> [--effort <e>] [--full] [--note-file <file>]
hl-flow --root <root> --run-dir <run> reviewed --id <ID>
hl-merge --root <root> --run-dir <run> --repo-dir <clone> --id <ID>
hl-verify --root <root> --run-dir <run> --repo-dir <clone> --id <ID> --wait
hl-flow --root <root> --run-dir <run> close    --id <ID>
```

| Command | Answers | Means |
|---|---|---|
| `prepare` | `prepared`, with `floor`, `criteria`, `has_verify`, `item_file`, `port`, `setup`, `missing_commands` | The worktree, branch and contract exist. `setup: ready` means the repository's setup ran in the worktree. `missing_commands`: criteria whose command is a program under a tool directory of the worktree (`.venv`, `venv`, `node_modules`, `.deps`) that is not there after the setup, with where it was looked for. Every build and review of the item would fail it: settle it before the build (manager prompt, step 1). Read from the criterion's prose, so read the criterion before you act on it |
| | exit 2, the item would get no port | It is routed with a look, or its floor asks for one, and its stream has no `port_base` (or you passed `--port none`). A look is of a served build: pass `--port <n>`, one no other stream uses. Nothing was made |
| | exit 3 `setup-failed` | `troubles.md`, "The machine" |
| `route` | the route and the item's floor | Refused below the floor, and without a reason. `--look yes` is refused for an item prepared with no port: `prepare --id <ID> --port <n>` first (it keeps the worktree), then route again |
| `spawn start` | `watch: <ID>:<phase>-<n>`, `brief_notes` | Pass the watch to `hl-wait --for`. `brief_notes`: lines below the script's own brief that went with it. The script cannot tell whose they are: not 0 when you added none means something else wrote the file |
| `built` | `built`, with `disputed` | The builder is closed and the pull request recorded. A non-empty `disputed` is a correction before any review. `protected_exempted`: protected paths the pull request changes that you exempted for this item; nothing else protected gets through |
| | exit 2 `sent-back`, with `watch` | The builder was told what to fix; wait on the same watch |
| | exit 2 `sent-back-twice` | The builder is closed; another build needs your reason in `rework-<n>.md` |
| `review` | `reviewing`, with `watch` and `checks` | The build is served and the reviewer started. With `--note-file`, the file's text is in the brief's own slot for a manager's note, and `manager_note` names the file (a review's brief cannot take notes below it: it is rendered once the build is served). In a run with `confirm: delta`, `confirm` says which review the script gave and why: `full: …`, or `delta: …`. Refused while the worktree is at another commit, or tracked files in it differ from the commit: the refusal names the commands that put it back |
| | exit 4 `checks-red`, with `checks_file` | A rework: the next build's brief points at that file |
| | exit 4 `conflicting`, with `next` | GitHub says the pull request conflicts with its base: it gets no checks and cannot merge, so nothing was served or reviewed. A rework: write `rework-<n>.md` as `next` says (merge `origin/<base>`, never rebase), then the next build |
| | exit 4 `written-before-review`, with `files` and `note_file` | The build's own Serve or Ready line wrote tracked files: a rework, and the next build's brief points at the note. `detail` names the spec key for a path that is harmless |
| | exit 3 `not-ready` or `setup-failed` | The build did not come up (`serve-<n>.log`): a rework; or the setup did not run |
| `reviewed` | `effective`, `fix_list`, `criteria_in_question`, `criterion_notes`, `checks`, `comment`, `dirty_before` | Act on `effective` (manager prompt, step 7). `criterion_notes`: findings of kind `criterion` that name no criterion, notes on the item's own text (its Scope, or an Accept line a correction replaced); nothing is corrected for them, and they go to the follow-ups like any finding. `dirty_before`: tracked files the spec names under `rewrites` that were rewritten before the reviewer started; they hold nothing up. `checks: conflicting`, with `next`: the pull request has come to conflict with its base since; a pass of this head cannot merge, so it is a rework as `next` says |
| | exit 2 with `problems` | That review does not count for a merge. With `checkout` among them it is a pass that never was a pass of the head: do what `next` says (give the attempt back and review again; the second time for an item, park it). Otherwise review again; that one stays counted. A review that did not pass stands whatever happened to its checkout |
| `hl-merge` | exit 0 merged; 2 refused (reasons listed); 3 lock held; 4 branch behind; 5 merge not completed (read `merge.json`) | After a merge read `attribution_on_base`: anything but `null` goes to the operator |
| `hl-verify` | exit 0 `verified`; 3 `not-yet` (without `--wait`); 4 `failed` | `--retry --note "<what changed>"` runs a recorded failure once more. `--fixed-by <run>:<ID>` settles one by the fix-forward that merged after it and verified. A Verify line that is itself wrong (it can never pass against a good deploy) is corrected by the operator in the backlog file and staged; then `hl-items --run-dir <run> verify-line --id <ID> --note "<what was wrong>"` loads it, and `--retry` runs it |
| `close` | `closed` | A merged item's worktree and local branch are gone |

- A protected path (the repository's `protected` in the spec) is refused by `built` and again by `hl-merge` for every item. A run whose repository's spec named `exemptable` patterns when the run was created may let one item change one such file: record that, with why, before its build, typed in your own shell tool (the command refuses unless it was started straight from a shell your `claude`, the claim's holder, started: never from a script, a line one of the kit's scripts runs, or a worker; and it records which session granted it): `hl-items --run-dir <run> exempt --id <ID> --path <file> --reason "<why this item, and no other>"`. It takes one file as `git diff --name-only` prints it (brackets are part of a name, never a pattern; `*`, `?`, `{`, `}` and `!` are refused), within both `protected` and the run's `exemptable`, and refuses a path the base branch holds as a directory. `--withdraw` takes it back. `built` and the merge both read the record as it stands: a withdrawn exemption stops the merge. The builder's and reviewer's briefs name the exempted file. A delta confirm still gives way to a full review for a fix that touches it. A run created without `exemptable` takes none, whatever the spec says now. Never record one to rescue a build that touched a protected file it had no reason to: that is a send-back. It is a decision of yours: a row in the ledger's Decisions, "decided by the manager".
- `hl-merge --dry-run` checks every gate and stops before the lock. `hl-merge … --release-own` at every session start frees a lock a dead merge left.
- The required checks are every check the base branch requires (its protection and its rulesets), each as it reported or `pending` while it has not. So `hl-wait --checks` waits for one that starts late, and after the grace answers `not_reported` with its name (`troubles.md`, "A required check never reports").
- `hl-merge` refuses a pull request that GitHub reports blocked while every required check is green, or while it has none: the branch requires something else (`troubles.md`, "GitHub says blocked"). `--allow-no-required-checks` does not lift this refusal.
- A build after the first is briefed from what is on record: the verdict that did not pass, `checks-<build>.md`, or `rework-<n>.md` that you write in the item directory when the reason is neither (a conflict with the base, a build sent back twice). Write facts in it, not a criterion.
- **A delta confirm** (a run with `confirm: delta`): once for an item, a small fix after a failed review gets a fresh reviewer that answers that review's fix list against the fix alone. The script decides; `--full` gets a full review, and nothing gets a delta it refuses. `reviewed` then also answers `confirms`, `answers` and `delta`. If `hl-merge` refuses the confirm: `troubles.md`, "A delta confirm".
- A second Claude build resumes the first builder's session by itself; `--fresh` forces a new one. A Cursor builder is always a fresh session.
- Taking an item up again: `hl-items --run-dir <run> set --id <ID> --status pending --reopen --note "<why>"` on a failed, parked or skipped item. One that had started comes back in progress where it stopped.
- A failed item whose work a repair item carried: once the repair has shipped (merged, promoted on a train, Verify passed), `hl-items --run-dir <run> set --id <ID> --status skipped --superseded-by <REPAIR-ID> [--superseded-run <repair's run dir>] --note "superseded by <REPAIR-ID>"`. It refuses before the repair has shipped. Then `hl-flow close` removes the failed item's worktree and `hl-spawn end-run` can close its run's workspace.

**A loose item** (criteria that judge prose, or none that can be run): `hl-spawn start --id <ID> --phase contract-draft --engine <builder>`; on its done file close it, then `hl-spawn start --id <ID> --phase agree --engine claude --set CONTRACT_PATH=<item dir>/contract-draft-<n>.md`. `AGREED`: `hl-items contract --id <ID> --from-draft <n>`. `REVISE`: `--from-amended agree-<n>.md`.

**Other worker commands:**

```
hl-spawn --root <root> --run-dir <run> brief  --id <ID> --phase <phase> [--engine <e>] [--set NAME=value] [--full] [--note-file <file>]   # render a brief to read it, or to add notes below it; start renders it again and sends the file only if it begins with that. A review's brief takes a note only through --note-file
hl-spawn --root <root> --run-dir <run> prompt --id <ID> --phase <phase> --n <n> --file <file in the item directory>
hl-spawn --root <root> --run-dir <run> close  --id <ID> --phase <phase> --n <n>
hl-items --run-dir <run> void --id <ID> --phase <phase> --note "<why>"
hl-items --run-dir <run> show --id <ID>
hl-serve --root <root> --run-dir <run> stop|status --id <ID>
```

`hl-spawn close` (and `hl-flow built`, `reviewed`, which close a worker) also kills what the worker's shells left running once its pane is gone; the note says what, and `<phase>-<n>.worker.json` keeps each pid and command under `reap`. A `warning` names a pid KILL did not end: read what it is before the next attempt. A note that a worker's shells were not written down means it was started by an older kit, and nothing it left was looked for.

Where the repository's spec names a `reference` directory, `hl-flow review` of an item routed (or floored) with a look serves that directory of the checkout beside the build, on a free port, in a tab of its own (`<ID> reference`), and the review brief gives the reviewer its address. `reviewed` and `hl-serve stop` stop both, each whatever became of the other: a refusal names what is still up, and the next `stop` tries only that. A reference that did not come up is noted under `reference` in `serve-<n>.json`, and the build is reviewed without it, as before.

## Pull requests from outside the program

```
hl-handoff --root <root> list
hl-merge   --root <root> --run-dir <run> --repo-dir <clone> --outside <PR> [--repo <owner/name>] [--dry-run]
hl-verify  --root <root> --run-dir <run> --repo-dir <clone> --outside <PR> --wait
hl-handoff --root <root> decline --repo <owner/name> --pr <PR> --reason "<why, and when it would fit>"
```

| Command | Answers | Means |
|---|---|---|
| `hl-handoff list` | `adopt`, `prs`, `errors` | The same as a save's `outside_prs_ready`, at any time |
| `hl-merge --outside` | exit 0 `merged` (record in `<run>/outside/<PR>/merge.json`), `already-recorded`, or `dry-run-passed`; 2 refused (reasons listed); 3 lock held; 4 `needs-update` (behind: `gh pr update-branch`, then again); 5 merge not completed | Refused unless program.json says `"outside_prs": "adopt"`, the pull request carries `rollout-ready` and not `rollout-held`, its steward's comment names the commit it vetted (`rollout-ready-head: <sha>`) and the head is that commit or that commit with only the base merged in, it is from a `secfix/`, `steward/` or `renovate/` branch of the repository itself (not a fork) into the base, changes no path the spec protects, has at least one required check and all of them green on an up-to-date head, and nothing merged or begun in that repository (an item, or another outside pull request) is waiting on its deploy check. Refused in a repository that runs a train (pass it over instead), from a run directory outside `<root>/runs`, and with `--allow-no-required-checks`. Exit 5 `merge-not-observed` or `merge-commit-unknown`: the merge may still land; its intent holds the repository until you run the same command again, which records it (or clears it, once the pull request is closed unmerged). After a merge read `attribution_on_base` as for an item |
| `hl-verify --outside` | exit 0 `verified`, `no-deploy` or `no-run`; 3 `not-yet` (without `--wait`); 4 `failed` | Reads the runs of the repository's deploy workflows (`deploys` in the spec's facts for it) on the merge commit, and where that run is missing or did not pass, a later run on the base whose commit carries the merge (`superseded` when it passed: that deploy shipped this change too). The deadline is `--deploy-wait` from the merge (90 minutes; never under 10). A skipped run is final. `no-deploy`: the spec names no deploy workflow. `no-run`: none ran by the deadline (path filters), so nothing deployed; a deploy its path filters leave out holds the repository until the deadline, so pass a shorter `--deploy-wait` (10 minutes at least) only when you know it will not run. `failed`: a deploy run failed or was cancelled with no later deploy carrying it, or was still running at the deadline: read it, and once it is fixed or run again, `--retry --note "<what changed>"`, which has a deadline of its own. Until it settles no item and no other outside pull request merges into that repository |
| `hl-handoff decline` | `declined` | `rollout-ready` is off, `rollout-held` is on, and your reason is a comment on the pull request, where the steward reads it. Refused for a pull request that does not carry `rollout-ready` (an item's own, for one) |

- `hl-merge … --release-own` at a session's start also frees a lock a dead outside merge of that run left; an item's own recovery never frees an outside merge's lock, nor the other way round (the lock names an outside merge's run `outside:<run>`).
- A steward never merges in a repository whose program has a live manager; a pull request it marks ready waits for you. When `adopt` is true, fold a green one into your next merge in that repository, or pass it over with the reason; never leave one unanswered across many cycles. When `adopt` is false (`adopt_off_because`), leave them all alone: `hl-handoff decline` refuses then too.
- Only a label put on, and a never-edited `rollout-ready-head:` line written, by a login in `outside_pr_vetters` count; the gate refuses when the loop's own token acts as one of those logins, unless program.json says `"outside_pr_shared_login": "accept"`. A repository's spec must say what deploys it (`deploys`, `[]` for nothing, each a file under `.github/workflows` on the base) before an outside pull request merges or is checked there.
- A red outside deploy holds every merge in its repository: fix it forward with one item recorded by `hl-items --run-dir <run> fixes-outside --id <ID> --pr <PR> --reason "<what it fixes>"` (typed in your own shell, refused otherwise, as `exempt` is; refused unless that pull request's deploy check is recorded failed; `--withdraw` takes it back), the only merge that passes that hold, and its merge answer says so; never re-run an older commit's deploy.
- A GitHub read that fails mid-merge exits 1 with a traceback: nothing was recorded, a begun merge's intent is kept, and running the same command again is safe.
- The label is set by a steward after its own research and the repository's required checks; it is not a review, and you do not review it. You decide only when it fits, and whether anything in flight conflicts with it.

## A train

For a repository whose runs answer `"merge": "train"` from `prepare`. Its items merge into `hl-train/<program>` on their review; the required checks run on a batch.

```
hl-train --root <root> --repo <owner/name> status
hl-train --root <root> --repo <owner/name> test [--note "<why now>"]
hl-train --root <root> --repo <owner/name> promote
hl-train --root <root> --repo <owner/name> repair
hl-train --root <root> --repo <owner/name> abandon --note "<why>"
```

| Command | Answers | Means |
|---|---|---|
| `status` | `ahead`, `items`, `due`, `full`, `open`, `red`, `verify_pending`, `base_moved` | `due`: `batch` items have merged since the last promotion and no batch is open. `full`: the train is at its `limit` and `hl-merge` will refuse the next merge into it |
| `test` | `testing`, with `batch`, `pr`, `items` | The train's head is pinned as `hl-batch/<program>/<n>` and its pull request into the base is open; the required checks run on it |
| | exit 4 `conflict`, with `files`, `repair_file` | The base moved and does not merge cleanly into the train. Run `repair_file` as an item in a run of its own, made with `--repair --land merge` |
| `promote` | `promoted`, with `items`, `verify`, `tree_is_the_tested_one` | The batch is merged into the base by a merge commit; run `hl-verify` for each item under `verify`. A `warning` (the base moved in the moment of the merge) goes to the operator at once |
| | exit 4 `red`, with `failed` and `links` | A required check failed; nothing was promoted and nothing closed. Not the batch's fault: re-run it once and `promote` again. Its own: `repair` |
| `repair` | `repair`, with `red_file`, `repair_file`, `repair_id` | The red batch is closed. Run the repair as an item in a run of its own, then `test` again. Refused unless the batch's checks have finished red |
| | exit 5 `pending` | The checks are still running |
| | exit 6 `base-moved` | The base moved after the test; the batch is closed. `test` again |
| | exit 3 | The merge lock is held: wait and run it again |
| | exit 2, with reasons | A commit on the train this loop did not merge, attribution in the batch, an earlier promotion whose Verify has not passed (unless it failed and this batch carries its fix-forward: an item of a `--repair` run whose text names it), a batch branch that was pushed to, another `hl-train` command still at work, a batch GitHub reports blocked although every required check it lists is green or it lists none (`troubles.md`, "A train") |
| `abandon` | `abandoned` | The open batch is given up: its pull request closed, nothing promoted |

- `promote --dry-run` checks every condition and stops before the lock.
- A repair's run: `hl-items --run-dir <root>/runs/repair-<n> init --root <root> --stream <stream> --run-id repair-<n> --repo <owner/name> --file <repair_file> --work "repair batch <n>" --repair`. `--repair` lets it merge into a train that is at its limit. A run that brings a conflicting base into the train adds `--land merge`: its pull request is merged whole.
- A `promote` that was cut short after GitHub merged is finished by running `promote` again; until then `test`, `repair` and `abandon` refuse.
- `hl-items --run-dir <run> drop-item-check --name <check> --note "<why>"` stops holding a run's items to a named check that their pull requests never start.
- `hl-verify` answers exit 3 `not-promoted` for an item whose batch has not been promoted. Its Verify command is given `HL_MERGE_SHA` (the item's own merge) and `HL_BASE_SHA` (the commit that carried it onto the base: the promotion).

## Waiting

One background wait per run you are working, never two on one run, started with the shell tool's background option. It exits, which wakes you, and prints what happened.

```
hl-wait --root <root> --run-dir <run> --for <ID>:<phase>-<n> [--checks <ID>] [--train <owner/name>] [--max-min 20]
```

| Event | Means | Do |
|---|---|---|
| `done` | the attempt's done file exists | the next `hl-flow` step |
| `checks` | every required check finished; `passed` and `head_sha` say how and on what; red checks are in `checks_file`. Or at once, with `conflicting` and `next`: the pull request conflicts with its base, so its checks will never report | green and a pass on file: merge. Red: a rework. `conflicting`: a rework as `next` says, whatever the checks or the review said |
| `batch` | every required check of the train's open batch finished; `passed` says how | `hl-train … promote`, whichever way it went |
| `time` | the minutes passed | save, wait again |
| `seat` | the watchdog placed the program on another Claude seat (`action: move`, `from`, `to`), or paused it because no seat has room (`action: pause`, `until`) | hand over, or pause: `troubles.md`, "The program's Claude seat" |
| `blocked`, `stopped`, `quiet`, `gone`, `limit`, `error` | | that entry of `troubles.md` |

`blocked`, `limit`, `stopped` and `quiet` are each reported once per occurrence. `seat` is reported on every wait while it holds, before anything else, and the answers of `hl-flow save` and `hl-claim beat` carry it as `seat` too. With no `--for` and no `--checks`, `--max-min N` is a plain timer.

## Runs and workspaces

```
hl-items --run-dir <run> init --root <root> --stream <stream> --run-id <id> --repo <owner/name> --file <ITEMS file> --work "<what the run completes>"
hl-items --run-dir <run> next | list
hl-spawn --root <root> --run-dir <run> end-run        # exit 2 until every item is merged and verified and nothing is open
herdr workspace list                                  # which workspaces exist; a run's carries its --work name
herdr agent read <agent> --source recent-unwrapped --lines 40
herdr agent send-keys <agent> esc
```

As of 2026-10-13: the comparison canary was not run (the operator's decision), and the first real use of round 2 was the rest of an app rebrand program. `subagents` is off in the template: on the mini a sub-agent ran in the background although the guard asks for the foreground, so a worker can finish before its sub-agent answers; leave it off until that is proven. `confirm: delta` has not run on the mini either.

`init --root` gives the run the program's policy (how premises and reviews work, the bounds, whether a rework may get a delta confirm), which it keeps for good. One bound counts no phase: `give_back`, how many passes over a changed checkout an item may have given back (1).

## The watchdog

```
hl-watchdog --root <root> status
hl-watchdog --root <root> arm --note "<why>"       # a manager should exist from now on
hl-watchdog --root <root> disarm --note "<why>"    # the program is complete or paused
hl-seat --root <root> status [--full-percent <n>]  # the Claude seats' readings, the seat in use, and what the watchdog would do; writes nothing
```

Armed, it starts a manager when nobody holds the program, when the holder's process is gone, or when the holder has not saved for 45 minutes. A release is a hand-over and is not held against you; three managers in a row that hand over with nothing moved in the run directories halt it, and so do three starts in an hour that were not hand-overs. `arm` again after looking.

Each tick it first decides which of the program's Claude seats (`claude_config_dir` and `claude_seats`) it runs on. It keeps the program on a seat with room: when the seat in use reaches `seat_full_percent` of its five-hour or weekly window and another seat has room, it places the program there; when no seat has room, it pauses the program and starts nothing until the first seat resets, then starts a manager there. It tells the operator each time, and you through `seat`. A release whose note starts with `seat` never counts as an idle hand-over. Disarming is never the answer to a pause.

## Capacity (check before every spawn)

```
memory_pressure | tail -1            # "System-wide memory free percentage"
sysctl vm.swapusage
uptime
```

Do not start another worker when free memory is under 20 % or swap is full. Measure before claiming a constraint, and write the numbers in the ledger. Two streams fit side by side on the host where it was measured (`host-facts.md`, Capacity); a third has not been measured.

Under 20 GiB free on the host's data volume (`df -g /System/Volumes/Data`), and Docker runs in a Linux VM on the host: run `sudo fstrim -v /var/lib/docker` inside that VM (through the runtime's own shell command). It is safe while containers run and hands blocks the VM has freed back to the host. Write both numbers in the ledger. A stuck Docker is in `troubles.md` (The machine).

## Secrets

```
hl-secrets --root <root> run -- <command…>   # the command gets GH_TOKEN and nothing else changes; exits as the command exits
hl-secrets --root <root> budget              # what a 1Password service account has left, and whether to hold; says when it does not apply; reads no secret
hl-secrets --root <root> status              # which secrets are held, how old, how many reads; never a value
hl-secrets --root <root> forget              # after a secret was rotated: the next request reads it again
hl-secrets --root <root> stop                # end the keeper; the next request starts another
```

- The keeper is one process for the program. It starts by itself the first time a script needs a secret and leaves after six hours without a request. It reads through `hl-read-secret`, or through the command `program.json` names in `secrets_reader`. It holds the GitHub token, the workers' Claude token (none where `worker_claude_ref` is `-`) and the ntfy password (none where no ntfy topic is set), and reads each again when it is twelve hours old. When that read fails it goes on with the one it has and tries again ten minutes later; `status` then lists the secret with `past_lifetime_since` and `last_error`.
- `budget` is for a program whose references are `op://` and read through a 1Password service account, whose daily allowance is shared with everything else on the host that reads through it. Where no reference in `program.json` is `op://`, or no service-account token is found, it runs nothing and answers, with exit 0, `"applies": false` and `why_not`, with `reads`, `hold` and `why` still there. Where it applies it also answers `account_is`, `account_remaining` and every limit under `limits`. Either way, hold when `hold` is true: the keeper's last read failed, or (where it applies) under 50 requests are left. Exit 2 is a refusal (a token and no `op`, a timeout, no counters printed): write what it said. If it prints text and not JSON, `op` changed its table: read the numbers from the text.
- A first read that failed is not tried again for ten minutes, and every script that needs that secret refuses meanwhile with what the reader said: `troubles.md`, "A secret cannot be read". `forget` and `stop` are not a way to try sooner: each try is a request, and a failed one may still be counted.
- `hl-secrets get` prints a secret. It is for a worker's pane (`hl-env.sh` calls it); never run it yourself, because what you run stays in your transcript.
- All of this holds for a program whose `program.json` says `"secrets": "keeper"`, which staging writes. Without that line (or with `"direct"`, the default) the scripts refuse with `GH_TOKEN is not set`, and each of them is then run as `hl-secrets --root <root> run -- <kit>/scripts/hl-flow …`; a worker's pane then reads each secret itself with `hl-read-secret`, and the watchdog with the program's reader, once per use.

## Gates and credentials

A gate that waits on an action is yours (the prompt's mandate). A one-shot action is a script, written next to the program's other bootstrap scripts, that reads each secret with the program's reader inside itself (`<kit>/scripts/hl-read-secret <ref>`, or `secrets_reader`) and prints none, checks who it is acting as before it writes, reads back what it wrote, and tees its output to a read-back log beside it:

```
root_env() {   # an AWS root credential, for IAM and one-time setup only
  local read=<kit>/scripts/hl-read-secret
  export AWS_ACCESS_KEY_ID=$("$read" 'op://<vault>/<aws-root-item>/AWS_ACCESS_KEY_ID')
  export AWS_SECRET_ACCESS_KEY=$("$read" 'op://<vault>/<aws-root-item>/AWS_SECRET_ACCESS_KEY')
  unset AWS_SESSION_TOKEN AWS_PROFILE
  [[ $(aws sts get-caller-identity --query Arn --output text) == "arn:aws:iam::<account>:root" ]] || exit 1
}
```

Have a sub-agent or a worker write and run it, with the evidence under `<root>/runs/gates/<gate>/`; then read the live state yourself, record the gate in the ledger's Gates table, and commit the script and its log by pull request. The reader only reads, so nothing the loop makes is written back to the secrets store: a short-lived user lives and dies inside its script.

## Telling the operator

```
hl-secrets --root <root> notify --title "<program>: <what happened>" --body "<one or two plain sentences>" [--priority high]
```

It posts to the program's ntfy topic with the password the keeper holds, and answers `"notified": true`. Exit 2 with the reason means the operator was not told; a program with no ntfy topic in `program.json` has no channel but your reply and the ledger's "Now".

## Never

- `herdr update`, `herdr server stop`, or anything that restarts the Herdr server while a program is live: every pane dies.
- Closing a workspace, tab or pane by hand, or starting a worker or a served build any other way than the scripts above.
- Killing by name or pattern. Read the full command line of one PID, confirm it is yours, kill that PID.
- `agent login`, `agent logout`, `agent --api-key …`, `claude` login or logout: one login replaces the account for every running agent.
