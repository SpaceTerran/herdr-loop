# Ledger: {{PROGRAM}}

The manager's record of this program, and all the next session will know. Keep "Now" short: it is read whole at every session start. The sections below it grow with the program and are read a row at a time. Live state (the run directories, GitHub, Herdr) wins over this file: when they disagree, fix this file.

Two files beside this one are written by `hl-flow save` from the run directories and are not edited or read at start: `LEDGER-ITEMS.md` (runs, items with their routes, builders and reviews, engines) and `FOLLOW-UPS.md` (what builders proposed and reviewers found: a sub-agent reads it for the defects that are the manager's to place; the rest is for the operator).

`hl-flow archive` moves what is settled out of this file and `FOLLOW-UPS.md`, once a sweep has read it: every Decision but the newest 40 and every gate its last row settled go to `LEDGER-ARCHIVE.md`, the follow-ups of items no longer in progress to `FOLLOW-UPS-ARCHIVE.md`. A line under Gates and under Decisions says what the archive holds. Find an older row with `grep -n '<what>' LEDGER.md LEDGER-ARCHIVE.md`; none of those files is edited by hand.

Times are UTC; read `date -u` before writing one.

## Now (rewrite this section before every session ends)

Written {{UTC}}. This section holds what is true now and nothing else: each line was checked against the run directories, GitHub or Herdr in the session that wrote it. What happened goes in Decisions or Lessons; what merged is in `LEDGER-ITEMS.md`.

**Live.** For each stream: the run and its workspace in Herdr, the item in flight, its step, the worker's name, and since when. For a stream with nothing in flight: what it waits on, since when, and what will end the wait. Or "Nothing is running." Name any workspace still open for a run that has ended, and what it waits on.

**In flight, lost if this session dies.** Background waits, sub-agents, anything that is not already in a run directory or pushed. Or "Nothing."

**Next, in order.**

1. …
2. …

**Owed by the operator, asked once.** What, and when it was asked. Or "Nothing."

**Clean up.** Worktrees, branches, panes, ports. Or "Nothing."

## Placements

Rewritten like "Now" when something here changes, never a line added for each session: it is read at every start.

- Manager: account, model, effort.
- Claude workers: account. Shared with: who else draws on that usage window.
- Capacity: the last measured figures.

## Gates

Conditions between files that no item holds.

| Gate | Before | Result | When | How it was checked |
|---|---|---|---|---|

## Decisions

One row per decision: those the manager made itself, marked "decided by the manager" with the reason (the operator may overrule them later), and those only the operator can make (money, a policy on accounts and sign-in, deleting data or removing a feature, access or credentials, a choice between product directions), each with its parked item. Other work continues.

| # | Item | Question | Recommendation | Asked | State |
|---|---|---|---|---|---|

## Lessons

Append, newest last, in plain sentences: what happened, how it showed, what to do. If a lesson changes how every program should run, say so: it belongs in the kit.
