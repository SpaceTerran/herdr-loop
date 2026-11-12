# Writing a backlog for the Herdr loop

For whoever writes the items and the spec. A program's own guide (the product, its design sources, its literals) sits beside its backlog files; this is what holds for every program.

An item becomes one brief, carried out in one unattended session, then reviewed by another session that reads the code and exercises the change. Nobody answers a question. What you write is the whole specification.

## The file

```
## HD-07 — Title

**Scope.** Everything a worker must know: what exists today, what it becomes, what must stay as it is, which paths the item may write.

**Accept.**
1. One observable result, with the command or the page that shows it and the value expected.
2. …

**Depends.** HD-03, HD-05.

**Floor.** look: every product shares this page.

**Verify.** `<one command that exits 0 once the merge has deployed>`
Wait. 15
```

- The heading is `## <ID> — <title>`. Ids are never reused or renumbered.
- `**Scope.**` and `**Accept.**` are required. `**Accept.**` is one unbroken block of lines `N. text`, at most ten; a blank line ends it. Commands go inline in backticks.
- `**Depends.**` names ids of the same run. An ordering between files is a gate in the spec. Either one names the output it needs from what it waits on ("Nothing waits to see whether it fixes itself", below).
- `**Verify.**` is one line: one command in backticks with no backtick inside it, then optionally `Wait. <minutes>` (1 to 60, default 15). The wait is a deadline counted from the merge, not a soak: the loop runs the command as soon and as often as needed and records a failure only when the deadline passes. Write a command that fails until the deploy has landed and then passes.
- Text above the first heading reaches no worker.

## Accept lists hold behaviour

A criterion says what the change does, as a command or a page with one expected value. Leave these out:

- **No suite line.** Not "`npm ci && npm run test && npm run build` exits 0", not "the full pytest suite passes". The repository's required checks run its suites once, on the pull request, and the merge is refused unless they are green on the commit that merges. A suite written into a criterion is run again by the builder and again by the reviewer: in the first real program that was 4 to 12 long test runs for one item, and most of the time a session was open.
- **No required-checks line**, and nothing with `--watch`. No worker waits on CI.
- **Nothing about a test runner's own output** ("the last line matches `<N> passed`"). It fails for the runner's settings, not for the build.
- A single test that shows the behaviour is fine: `pytest tests/web/test_shell.py::test_title -q exits 0`.

Staging prints a remark for each criterion of these kinds (`accept_remarks`). It refuses nothing: an older backlog runs as it is.

What does belong:

- **A literal with its baseline.** "`grep -c 'backdropFilter' src/Shell.tsx` prints `0` (it prints `2` at `503719c`)". A premise that has gone stale is then caught before anything is built.
- **A browser observation with one value**, at a 1280×800 viewport, as an expression: "at `/app/alerts`, `getComputedStyle(document.querySelector('[data-testid=\"…\"]')).backgroundColor` is `rgb(20, 17, 28)`". Where color matters, one for each mode.
- **The reference.** An item that changes what a person sees names the reference page or file it is to match, and the modes. The reviewer compares the served page with it in every mode named, measured values and the whole page by eye. Do not pin every pixel in criteria; name the reference.
- A value the item pins for something the build will produce (a hash, a count) is the item's: say how it was produced.

## Name the tests the change breaks

An item that changes behaviour that already exists (a message, a status code, the shape of an answer, a default) breaks the tests that assert the old behaviour, and an item that adds a route, a table, a task or a file breaks any test that holds a list of them. Those tests are often far from the code: on a help-desk catalog program 20 of 21 red test runs (2026-11-01 to 11-09) failed in a test file the pull request had not touched, and those items took a median 98 minutes against 36 for the rest.

- **Before you write the item, search the tests for what it changes** (`grep -rn` for the function, the route, the message), and name what you find in the Scope: "`tests/test_ai_usage.py` asserts the old error and is updated with this change."
- **Name the repository's inventory tests once, in the spec**, among its `facts`: "a new route is added to the list in `tests/test_audit_inventory.py`". Every builder there is then told.
- **Never write a Scope sentence or a criterion that keeps the builder from a test the change breaks** ("touch no test", "the diff is one file", a write list that leaves the test out). The builder is told to update such a test wherever it lives, and a criterion that forbids it sends the item to a correction first. One item of that program went red on two such tests, and each was then fixed by a one-item repair run of its own, three in all, where one push to the same pull request would have done.
- A test you want added for the new behaviour is a criterion like any other: one test, named, as in "Accept lists hold behaviour" above.

## Floors

An item's author knows things the manager cannot see from the text. A floor says what the item requires, whatever the manager would choose; the scripts refuse a route below it, and the manager may go above.

```
**Floor.** premise, look: the shared sign-in page.
```

| Floor | Requires | Use it for |
|---|---|---|
| `premise` | an independent check of the criteria against the code before the build | criteria that lean on live values, or an item where a wrong criterion found late is expensive |
| `look` | a builder with a browser, and a review that compares the served pages with the reference in every mode | anything a person sees whose paths or title would not say so |
| `sonnet` | a Sonnet builder from the first build (the default for anything but a clearly mechanical item) | sign-in, money, infrastructure, a shared live surface |

Words after the colon are for people. Without a floor the manager routes the item from its text.

## Size

An item pays a fixed cost whatever its size: a worktree, a brief, a pull request, a round of required checks, a review, a merge, a deploy check. In the first program a builder's own time on an item was a few minutes of a half-hour total. So do not cut work thin. Two changes that share their files, their reference and their review belong in one item. Keep an item to one coherent change that one review can hold: a page and its own components, not "and also". A larger item puts more at risk in a failed review; where two parts could fail independently and neither needs the other, they are two items.

## The spec says what is true of the repository, once

`program.spec.json` sits beside the backlog files (`program.spec.template.json` in the kit). Beyond the streams and the prose for the manager's prompt, it carries each repository's facts. They reach every brief and script that needs them, so no item repeats them and no manager copies them from an older item's folder:

| Fact | What it is | Who uses it |
|---|---|---|
| `facts` | sentences every worker in the repository is told: a tool version CI uses that the host does not, a quirk of the test setup, a condition of the test host that is known and is not a finding | every brief |
| `setup` | one command line that makes a fresh checkout ready, writing only paths git ignores | `hl-flow prepare` runs it before any worker starts, and again before a review when an input changed |
| `setup_inputs` | the files that command's result depends on (lockfiles, requirement files) | the same |
| `ci_covers` | what each required check runs | the review brief: the reviewer does not run those again |
| `not_in_ci` | suites no required check runs, as commands | the review brief: where the diff touches what one covers, the reviewer runs that part |
| `seen` | glob patterns of the paths that hold what a person sees | an item routed without a look whose diff touches one is not reviewed until it is routed with one |
| `protected` | glob patterns of the paths no item may change (generated or pinned files; a repository whose workflow is generated from a central template lists that workflow, such as `.github/workflows/CICD.yml`) | a pull request that changes one is sent back to its builder, and refused at the merge, unless the manager exempted that one file for that one item (`hl-items exempt`), which only `exemptable` allows |
| `exemptable` | glob patterns of the protected paths the manager may exempt for one item (`hl-items exempt`); left out, none may be. Name as few as the program needs: `.github/workflows/CICD.yml` belongs here only if an item of the program exists to change it | fixed into each run when it is created; `hl-items exempt` refuses a path outside it, and `hl-merge` counts no exemption outside it |
| `reference` | one directory of the repository holding the pages a look is compared with (`brand`), as a relative path | `hl-serve` serves that directory of the item's checkout beside the build of an item a person looks at, and the reviewer is told its address: the browser tool refuses `file://`, so without it a reference is read as source |
| `rewrites` | glob patterns of the tracked paths the repository's own setup or served build rewrites by itself (a lockfile an install touches, `next-env.d.ts`); none unless you know one; a pattern that matches every path is refused at staging | `hl-flow review`: a review starts over such a file and notes it, and over no other tracked file that differs from the commit |

| `train` | `{}` or `{"batch": 5, "limit": 15}`: the repository's items merge into an integration branch on their review, and its required checks run on batches of them (below) | `hl-items init`, `hl-merge`, `hl-train` |
| `item_checks` | with a train: names of the quick checks an item's own pull request must still show green; may be left out | `hl-merge`, `hl-wait --checks` |

`settings` in the spec replaces a program's defaults (`program.template.json`). `subagents` is `false` there: sub-agents are not proven on the mini (one ran in the background although the guard asks for the foreground), so turn them on only for a program that is there to prove them. One that depends on these facts: `"confirm": "delta"` lets a small fix after a failed review be confirmed by a fresh reviewer against that review's fix list, in place of a second full review. `seen` and `protected` then decide more: a fix that touches a `protected` path, or a `seen` path the failed review did not look at, gets the full review. Leave it out for a second full review every time.

**Nothing may rewrite a tracked file while a review runs.** A review is read against its checkout as it was when its reviewer started. A tracked file that changes after that (a lockfile an install rewrites, generated code, `next-env.d.ts`), or that the served build writes as it is stopped, makes a pass there not count. The reviewer cannot put back what the build writes as it stops. The first such pass of an item is given back and reviewed again; the second parks the item for the operator, and nothing lifts that at the merge. A file the setup or the served build rewrites before the reviewer starts stops the review before it begins, and the build goes back to its builder, unless the spec names the path under `rewrites`. Name a path there only when the rewrite is harmless: the build under review is then not exactly the commit, and after a failed review the builder has to put the file back before its rework is accepted. In every case the cure is in the repository, as an item of its own before the program starts: stop tracking the file (`git rm --cached <path>`) and ignore it.

A fact that is wrong misleads every item: check each against the repository at the commit the backlog was written for, and say which commit in the program's own guide.

A stream with `port_base` gets each item's port worked out (the base plus the item's number). A stream that serves nothing leaves it out; an item of such a stream that a person looks at is then refused a look until the manager gives it a port (`hl-flow prepare --port <n>`), so a stream with any page to look at has a `port_base`.

## Look at the repository's CI before the program starts

Every item pays for a round of the repository's checks and for its deploy, and a program merges dozens of items: twenty minutes of checks is more than a day of a program's time. CI also grows with every item, a little each time. On the help-desk program its required CI check took 9 minutes on 2026-10-23 and 64 on 2026-11-02, and items went from about an hour to two and a half, before anyone asked why. So before staging, for each repository the program works:

- **Where the jobs run.** In the author's organisation, tests, checks and plans run on a self-hosted fleet (`runs-on: [self-hosted, <label>]`), and only applies and deploys stay on GitHub's hosted runners (a mobile app backend's split since 2026-07-29). A job on `ubuntu-latest` spends the organisation's included Actions minutes, and once those are spent a hosted job fails in seconds with no steps. Two repositories of the help-desk program were still on hosted runners until 2026-11-02 and used most of October's minutes.
- **How long the checks take**, every check a pull request runs and not only the required ones, from the last few merged pull requests, and which check is the slowest.
- **Whether the suite runs in parallel** (`pytest-xdist`, Vitest's threads). A suite that runs one test at a time grows in time with every test an item adds.
- **Whether the deploy runs the suite again.** With "require branches to be up to date before merging" on the base, the tree that merges is the tree the pull request's checks passed, and the deploy needs only its build and a health check.

If the checks take more than about 15 minutes or any answer above is wrong, the program's first items fix that, ahead of its features. Once it runs, `hl-flow save` reports the checks and deploy time of each repository's last merged items against `ci_budget_minutes` (hl/pace.py), and the manager acts when one goes over.

## A repository with a train

Where a repository's required checks take minutes, a round of them for every item is most of an item's time: on a mobile app backend the first program's builders worked for 26 of the 115 minutes their sessions were open, and the rest was tests, installs and waiting for checks. With `"train": {}` among the repository's facts, an item merges into an integration branch (`hl-train/<program>`) as soon as its review passes, the next item is built on top of it, and the required checks run once for a batch of merged items. A green batch is merged into the base by one merge commit; a red one becomes a repair item, and nothing reaches the base until a batch is green. The base's tree is then always one the full checks passed as a whole.

What that asks of the repository and of you:

- **Its long workflows should run only for pull requests into the base** (`on: pull_request: branches: [<base>]`). A workflow that runs for a pull request into any branch still runs for every item's pull request into the train: nothing waits for it, but it holds the runners the batch needs. Change that in the repository before its first train, as an item of its own.
- **Protect the base with "require branches to be up to date before merging" if you can.** With a train only batches merge into the base, so the rule costs nothing, and GitHub then refuses a batch whose base moved after its checks ran. Without it the script closes such a batch when it sees the move, but a push that lands in the second between its last look and GitHub's merge can only be reported afterwards, not prevented.
- **`item_checks` names only checks that do run for a pull request into the train**, and only quick ones (a linter, a secret scan). A name that never reports there stops every item.
- **`ci_covers` matters more.** The reviewer is told the full checks have not run and is asked to run the part of them the diff touches; it can only do that well if the spec says what they run.
- **A Verify line is run after the batch is promoted**, and its wait counts from the promotion. Write it against what is live (a page, a response), or against the promotion: the command is given `HL_BASE_SHA`, the commit that carried the item onto the base, and `HL_MERGE_SHA`, the item's own merge. A run triggered by a push to the base belongs to `HL_BASE_SHA`.
- **`batch`** is the number of merged items at which the manager is told a test is due (it may test sooner, and will after a risky merge); **`limit`** is the number past which nothing more merges untested. Smaller batches find a failure sooner and among fewer items; larger ones spend fewer rounds of checks. Five and fifteen are where to start.

A repository whose checks take seconds gains nothing from a train: leave it out.

## Gates are the manager's

A gate is a condition between files, or a step outside any item (an IAM grant, a secret written, DNS tokens pasted in, a build uploaded). Write each one for the manager to get done, with the exact procedure and the evidence that proves it: the manager runs it through a sub-agent or a worker, reads the live state, and records it. Do not give a gate to "the authoring session", a laptop session or the operator, and do not put a credential out of the manager's reach in `PRODUCTION_BOUNDARY`: the manager may use any credential the program's secrets store holds (the AWS root credential for IAM and one-time setup), touch any repository the step needs by pull request, and upload to App Store Connect. A gate held for a separate session idles the program for hours on a step the manager could have done in minutes (a help-desk feature program, 2026-10-16: three SES gates, an IAM exception, a secret and an upload waited on a laptop that was not running).

Only a step bound to the operator's own person goes to the operator: their device, their own sign-in or second factor, a dashboard with no API that only their login reaches (a Stripe restricted key), money, submitting an app for App Review. Say why in the gate's row, and keep the rest of the gate the manager's. A proof that "the operator" would make by hand (a password-reset mail, a sign-up) is made by the manager with an address whose inbox an agent can read.

Two limits the procedure has to respect, where the loop host's access to the secrets store is read-only (as it was on the author's): nothing on the host can write a new secret there, so a short-lived user lives and dies inside the one script that uses it (or its key goes to AWS Secrets Manager); and a secret is read inside a script by the program's secrets reader (`scripts/hl-read-secret <ref>`, or the command `secrets_reader` names in `program.json`), never printed or typed on a command line. Staging remarks on prompt prose that hands a gate to a person (`gate_remarks`); it refuses nothing.

## Nothing waits to see whether it fixes itself

The operator's rule (2026-11-10): nobody waits for a queue, a clock or a reading to see whether something fixes itself. That day every stream of the help-desk catalog program stood idle for about four hours. One item had been written to be built only "if the Renovate queue does not drain in a day", and its run's header held three unrelated items "until run `hd-lock` closes". The manager did as the text said. An account security sweep (2026-10-27) and an assistant bot's dependency pin (2026-11-02) had waited the same way, each on a clock someone wrote down.

- **An item is built, or it is not written.** Never write an item that is built only if some later reading says so ("if the queue has not drained in a day, raise the limits"). Write it unconditional, or do not write it. Something out of date (a dependency, a pin, an image, an action) is an item now.
- **A gate is a reading, taken when the gate is reached.** Do not give one a clock ("after 24 h", "next read 11-11", "T+1 day", "after tonight's backup", a soak, a queue left to drain). Name what the wait would catch, and write the procedure that reads it. The only gate that keeps a clock protects people: a user who could be locked out, data that cannot be restored. Say so in its row.
- **An edge between runs names what it needs from the other run.** A `Depends` on an item of another run, a gate between files, or a header line that orders a run after another, says which output of the other run it needs: a change it builds on, a table or a key it reads, a file it edits after the other does. In a `Depends` line, put the reason after the id: `**Depends.** HD-16 (the shared locker this item edits).` Every id in that line is read as a dependency, so the reason names no other. An order with no such output (a run "after" another because it was written later, or to keep things tidy) is not an edge: leave it out, and the run starts as soon as its stream is free.
- **The one wait an item carries is its Verify's `Wait.`**: a deadline counted from the merge, at most 60 minutes, not a soak (see "The file").

Staging remarks on each wait, length of time and run edge it finds in the spec's prose and in the backlog files the spec names (`clock_remarks`): in each file's header, and in each item's text but its Verify line. In an item's text it looks only for words that say a wait outright and for run edges; a length of time there is the product's own timing (a reminder 3 days after a request) as often as a wait. It refuses nothing. Once the program runs, `hl-flow save` lists for the manager each open run, item and gate that still says such a wait (`clocks`).

## What the manager decides, so you need not

Which engine builds and at what effort, whether a premise check runs (unless you floor it), whether the review looks (unless you floor it), and what a failure means. Write the item so that those are easy calls: say what it changes, name the paths, and say plainly when something that looks like backend work shows on a page.
